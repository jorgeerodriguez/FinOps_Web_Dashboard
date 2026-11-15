"""Bottom-up forecast by ANY grouping (NO Streamlit; shared by the page, the report and the tests).

The cloud bill is cut by a chosen field (account / project, project code, owner, account family, environment, service name, lifecycle status,
cloud service, cost center, provider x environment), every group is forecast on its own with the chosen model, and the groups add up to the
provider total: spend with no value for the field is kept as its own "(not set)" line and the smallest groups are summed into "All other",
so nothing is dropped. The default (account / project, Auto, 12 months) is exactly the table the page has always shown.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from . import models as M
from .nextyear import PROVIDERS, SP_NAME, LineForecast, NextYear, _rule_gap, forecast_line
from .owners import MISSING, UNASSIGNED
from .schema import CLOUD

DIMS = {"account": "Account / project", "project_code": "Project code", "owner": "Owner", "family": "Account family",
        "environment": "Environment", "provider_env": "Provider × environment", "service_name": "Service name",
        "status": "Lifecycle status", "service": "Cloud service", "cost_center": "Cost center"}
NOT_SET = {"project_code": MISSING["project_code"], "service_name": MISSING["service_name"], "cost_center": MISSING["cost_center"],
           "environment": MISSING["environment"], "status": MISSING["status"], "owner": UNASSIGNED}
WINDOWS = {"Last 12 months": 12, "Last 18 months": 18, "Last 24 months": 24, "All months": 0}
SCOPES = ["AWS", "GCP", "AWS + GCP"]
DIM_HELP = {
    "account": "One line per AWS account / GCP project.",
    "project_code": "Cost by project code from the owner files; spans AWS and GCP.",
    "owner": "Cost by the person accountable (owner files).",
    "family": "Accounts that belong together (e.g. prod + non-prod) rolled up.",
    "environment": "prod / non-prod / shared from the owner files.",
    "provider_env": "Each cloud split by environment.",
    "service_name": "Business service name from the owner files (about 4 in 10 dollars have none yet).",
    "status": "Lifecycle status of the account / project (Active, Idle, Deleted ...).",
    "service": "The cloud provider's own service (EC2, BigQuery ...).",
    "cost_center": "Cost center from the owner files (most spend has none yet).",
}


def available_dims(df: pd.DataFrame) -> list[str]:
    return [d for d in DIMS if ("environment" in df if d == "provider_env" else d in df)]


def group_key(rows: pd.DataFrame, dim: str, scope: str) -> pd.Series:
    """The group each billing row belongs to, as text."""
    if dim == "account":
        k = rows["account"].where(~(rows["provider"].eq("GCP") & rows["account"].eq("(no project)")), "Google Support (no project)")
        return k + " (" + rows["provider"] + ")" if scope == "AWS + GCP" else k
    if dim == "provider_env":
        return rows["provider"] + " · " + rows["environment"].astype(str)
    return rows[dim].astype(str)


def providers_of(scope: str) -> list[str]:
    return PROVIDERS if scope == "AWS + GCP" else [scope]


@dataclass
class BottomUp:
    dim: str
    label: str
    scope: str
    model: str
    window: int
    index: pd.DatetimeIndex                       # the months the lines were trained on
    fy: int
    months: pd.DatetimeIndex
    lines: list[LineForecast] = field(default_factory=list)
    table: pd.DataFrame = None
    board: pd.DataFrame = None                    # backtest error by line and model
    topdown: float = float("nan")
    added_back: float = 0.0                       # billing gaps added back (Savings Plans + reviewed rules), summed over the window
    not_set: str | None = None
    not_set_share: float = 0.0                    # share of the last 12 months with no value for this field
    scale: float = 1.0                            # factor applied to every line when reconciled to the top-down total
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return float(self.table[f"FY{self.fy} forecast"].sum()) if self.table is not None and len(self.table) else 0.0

    @property
    def models_used(self) -> Counter:
        return Counter(lf.model for lf in self.lines)

    @property
    def n_notes(self) -> int:
        return sum(1 for lf in self.lines if lf.note)


# ------------------------------------------------------------------------------------------ build
def _scope_rows(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    return df[(~df["is_partial"].astype(bool)) & (df["category"] == CLOUD) & df["provider"].isin(providers_of(scope))]


def window_index(df: pd.DataFrame, ny: NextYear, scope: str, window: int) -> pd.DatetimeIndex:
    first = _scope_rows(df, scope)["date"].min()
    start = first if not window else max(ny.last - relativedelta(months=window - 1), first)
    return pd.date_range(start, ny.last, freq="MS")


def _pivot(df: pd.DataFrame, ny: NextYear, dim: str, scope: str, idx: pd.DatetimeIndex):
    """(window rows with their group key, months x groups matrix after the billing-gap add-backs, total added back)."""
    provs = providers_of(scope)
    rows = _scope_rows(df, scope)
    w = rows[rows["date"].isin(idx)]
    w = w.assign(_k=group_key(w, dim, scope))
    acc = w.pivot_table(index="date", columns="_k", values="cost_net", aggfunc="sum").reindex(idx).fillna(0.0)
    added = 0.0
    for p in provs:                                                       # Savings-Plan fee gaps go to the group that pays them
        wp = w[(w["provider"] == p) & (w["service"] == SP_NAME)]
        sp = wp.groupby("date")["cost_net"].sum().reindex(idx).fillna(0.0)
        typical = sp[sp > 0].median() if (sp > 0).any() else 0.0
        gap = (typical - sp).where(sp < 0.5 * typical, 0.0) if typical else pd.Series(0.0, index=idx)
        paid = wp.groupby("_k")["cost_net"].sum()
        if gap.sum() > 0 and paid.sum() > 0:
            for k, share in (paid / paid.sum()).items():
                acc[k] = acc[k] + gap * share
            added += float(gap.sum())
    rules = ny.rules if ny.rules is not None else pd.DataFrame()
    if len(rules):
        for r in rules[rules["action"].str.strip().str.lower() == "add_back"].itertuples():      # reviewed add-backs go where that line sits
            if r.provider not in provs:
                continue
            g = _rule_gap(rows, r, idx)
            line = w[(w["provider"] == r.provider) & (w["account"] == r.account) & (w["service"] == r.service)].groupby("_k")["cost_net"].sum()
            if g.sum() > 0 and line.sum() > 0:
                for k, share in (line / line.sum()).items():
                    acc[k] = acc[k] + g * share
                added += float(g.sum())
    return w, acc, added


def leaderboard(lines: list[LineForecast]) -> pd.DataFrame:
    """Backtest error (MAE as a share of the exit run-rate) per line and model, plus a spend-weighted row over the lines each model could be tested on."""
    cols = [m for m in M.MODELS if any(m in lf.scores and np.isfinite(lf.scores[m]) for lf in lines)]
    rows, anchors = {}, {}
    for lf in lines:
        a = float(lf.actual.tail(3).mean())
        anchors[lf.name] = a
        rows[lf.name] = {m: (lf.scores[m] / a if a > 0 and m in lf.scores and np.isfinite(lf.scores[m]) else np.nan) for m in cols}
    b = pd.DataFrame.from_dict(rows, orient="index", columns=cols)
    wt, n = {}, {}
    for m in cols:
        ok = [lf for lf in lines if m in lf.scores and np.isfinite(lf.scores[m]) and lf.actual.tail(3).mean() > 0]
        n[m] = len(ok)
        den = sum(lf.actual.tail(3).mean() for lf in ok)
        wt[m] = sum(lf.scores[m] for lf in ok) / den if den else np.nan
    b.loc["All lines (spend-weighted)"] = pd.Series(wt)
    b["Used"] = [next((lf.model for lf in lines if lf.name == i), "") for i in b.index]
    b.attrs["scored"] = n
    b.attrs["lines"] = len(lines)
    return b


def run(df: pd.DataFrame, ny: NextYear, dim: str = "account", scope: str = "AWS", model: str = "Auto", window: int = 12,
        top_n: int = 12, reconcile: bool = False) -> BottomUp:
    """Forecast every group of `dim` inside `scope` (AWS, GCP or AWS + GCP). `model` is "Auto" or one of models.MODELS;
    `window` is how many months to train on (0 = all billed months). `reconcile` scales every group by one factor so the groups add up to the
    top-down Base forecast of the same scope (proportional reconciliation: keeps each group's share, changes the total)."""
    idx = window_index(df, ny, scope, window)
    w, acc, added = _pivot(df, ny, dim, scope, idx)
    out = BottomUp(dim, DIMS[dim], scope, model, window, idx, ny.fy, ny.months, added_back=added)
    if window and len(idx) < window:
        out.notes.append(f"Only {len(idx)} billed months exist, so the model was trained on those.")
    if ny.adjusted:
        late = [p for p in providers_of(scope) if (ny.effects.get(p, {}).get("trend_since") is not None and ny.effects[p]["trend_since"] > idx[0])]
        if late:
            out.notes.append(f"{' and '.join(late)}: the Cloud Services trend is measured from {ny.effects[late[0]]['trend_since']:%b %Y} to skip the launch ramp, "
                             "but these lines are trained on the whole window, ramp included.")
    tot = acc.sum()
    ns = NOT_SET.get(dim)
    order = tot.sort_values(ascending=False).index
    top = list(order[:top_n])
    if ns in tot.index and tot[ns] > 0 and ns not in top:
        top = sorted(top + [ns], key=lambda c: -tot[c])
    rest = [c for c in order if c not in top]
    force = None if model == "Auto" else model
    lines = [forecast_line(a, acc[a], ny.horizon, force) for a in top if acc[a].sum() > 0]
    if rest:
        lines.append(forecast_line(f"All other ({len(rest)})", acc[rest].sum(axis=1), ny.horizon, force or "Damped trend"))
    out.topdown = float(sum(ny.svc[p].loc[ny.months, "Base"].sum() for p in providers_of(scope)))
    raw_total = sum(lf.forecast.loc[ny.months].sum() for lf in lines)
    if reconcile and raw_total > 0 and out.topdown > 0:
        out.scale = out.topdown / raw_total
        lines = [replace(lf, forecast=lf.forecast * out.scale, lo=lf.lo * out.scale, hi=lf.hi * out.scale) for lf in lines]
        out.notes.append(f"Reconciled: every group scaled by {out.scale:.3f} so the groups add up to the top-down Base forecast.")
    out.lines = lines
    n_acc = w.drop_duplicates(["_k", "provider", "account"]).groupby("_k").size()
    names = {a: int(n_acc.get(a, 0)) for a in top}
    recs = []
    for lf in lines:
        exit_rr = lf.actual.tail(3).mean()
        fyv = lf.forecast.loc[ny.months].sum()
        rec = {"_name": lf.name, "Last 12 mo": lf.actual.tail(12).sum(), "Exit run-rate /mo": exit_rr, f"FY{ny.fy} forecast": fyv,
               "Low": lf.lo.loc[ny.months].sum(), "High": lf.hi.loc[ny.months].sum(), "Δ vs exit × 12": fyv - exit_rr * 12,
               "Model": lf.model + (" (capped)" if lf.capped else "") + (" (floored)" if lf.floored else "") + (" †" if lf.note else ""),
               "Confidence": f"{lf.confidence} (±{lf.mape:.0%})" if np.isfinite(lf.mape) else lf.confidence}
        if dim != "account":
            rec["Accounts"] = names.get(lf.name, int(sum(n_acc.get(a, 0) for a in rest)))
        recs.append(rec)
    t = pd.DataFrame(recs).set_index("_name") if recs else pd.DataFrame()
    if len(t):
        t.index.name = "Account / Project" if dim == "account" else DIMS[dim]
        t.insert(1, "Share", t["Last 12 mo"] / t["Last 12 mo"].sum())
        if dim != "account":
            t.insert(2, "Accounts", t.pop("Accounts"))
    out.table = t
    out.board = leaderboard(lines) if lines else pd.DataFrame()
    if ns in acc.columns:
        t12 = acc.tail(12).sum()
        out.not_set, out.not_set_share = ns, float(t12.get(ns, 0.0) / t12.sum()) if t12.sum() else 0.0
    return out


# ------------------------------------------------------------------------------------------ drill-down
def drill(df: pd.DataFrame, ny: NextYear, bu: BottomUp, group: str, by: str = "service", n: int = 10) -> pd.DataFrame:
    """What is inside one group: its cloud services or its accounts / projects, each forecast with the same model and window."""
    rows = _scope_rows(df, bu.scope)
    rows = rows[rows["date"].isin(bu.index)]
    inside = rows[group_key(rows, bu.dim, bu.scope).eq(group)]
    if inside.empty:
        return pd.DataFrame()
    key = group_key(inside, "account", bu.scope) if by == "account" else inside["service"]
    m = inside.assign(_k=key).pivot_table(index="date", columns="_k", values="cost_net", aggfunc="sum").reindex(bu.index).fillna(0.0)
    m = m[m.sum().sort_values(ascending=False).index[:n]]
    force = None if bu.model == "Auto" else bu.model
    recs = []
    for name in m.columns:
        if m[name].sum() < 100:
            continue
        lf = forecast_line(name, m[name], ny.horizon, force)
        recs.append({"Account / project" if by == "account" else "Service": name, "Last 12 mo": m[name].tail(12).sum(), "Exit run-rate /mo": m[name].tail(3).mean(),
                     f"FY{ny.fy} forecast": lf.forecast.loc[ny.months].sum(), "Model": lf.model + (" †" if lf.note else ""), "Confidence": lf.confidence})
    return pd.DataFrame(recs).set_index("Account / project" if by == "account" else "Service") if recs else pd.DataFrame()
