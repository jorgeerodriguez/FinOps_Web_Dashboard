"""Savings ledger and owner scorecard: numbers, figures and tables with NO Streamlit in them.

A *saving* is a sustained step down in a line's monthly cost, measured against that line's own earlier peak:

  peak      highest 3-month average of the line, ending at least 3 months before the latest billed month
  now       average of the latest 3 billed months
  qualifies drop >= materiality ($/month), now <= 85% of peak, and none of the latest 3 months back near the peak
  class     Reduced   the line still runs, smaller                 -> a saving
            Ended     now < 10% of peak                            -> a shutdown (confirm it was intended)
            One-off   one month carried >= 70% of the peak window  -> never recurring cost, NOT a saving
            Check     some (not all) of the latest 3 months are ~$0 -> likely a billing gap, NOT a saving
  realized  what the line would have cost had it stayed at its peak level, minus what it actually cost, since the step down
  erosion   a line that fell and then climbed back: the share of the saving that has returned

Before measuring, billing gaps are added back (the reviewed `add_back` rules in anomaly_rules.csv, and Savings Plans fee-timing gaps),
each filled from the line's own neighbouring months, so a bill that did not arrive is never mistaken for a saving or for savings coming back.

Cost data only: nothing here says WHY a line fell. Every row needs its owner to confirm."""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .history import History
from .ui import COLORS, TEMPLATE, money, pct

MATERIALITY = 5_000            # $/month
SKIP = {"Savings Plans for AWS Compute usage"}      # billed in lumps with gaps; handled by the Next Year normalisation
CLASS_COLOR = {"Reduced": "#15803d", "Ended": "#64748b", "One-off": "#b45309", "Check": "#7c3aed"}
CLASS_HELP = {"Reduced": "Still running, costs less: a saving", "Ended": "Near zero now: a shutdown to confirm",
              "One-off": "A single spike that did not repeat: not a saving", "Check": "Some recent months are $0: possible billing gap, not a saving"}


# --------------------------------------------------------------------------------------- series
ALL = "All clouds"                     # provider label of a line that spans both clouds (the project-code lens)
CROSS = {"project_code"}               # lenses whose lines are not tied to one provider


def series(df: pd.DataFrame, level: str) -> pd.DataFrame:
    """Months x (provider, name) for every line, missing months as $0. A project code is ONE line across both clouds, so moving spend
    between its accounts (or between AWS and GCP) nets out instead of reading as a saving on one line and new spend on another."""
    if level in CROSS:
        p = df.pivot_table(index="date", columns=level, values="cost_net", aggfunc="sum")
        p.columns = pd.MultiIndex.from_product([[ALL], p.columns])
    else:
        p = df.pivot_table(index="date", columns=["provider", level], values="cost_net", aggfunc="sum")
    return p.reindex(pd.date_range(df["date"].min(), df["date"].max(), freq="MS")).fillna(0.0)


def dim_of(df: pd.DataFrame, col: str) -> dict:
    """(provider, account) -> the value of `col` carrying most of that account's last 12 months."""
    d = df[df.date > df.date.max() - pd.DateOffset(months=12)]
    g = d.groupby(["provider", "account", col]).cost_net.sum().reset_index().sort_values("cost_net", ascending=False).drop_duplicates(["provider", "account"])
    return {(r.provider, r.account): getattr(r, col) for r in g.itertuples()}


def local_gap(v: pd.Series, months: list[pd.Timestamp] | None = None, window: int = 3) -> pd.Series:
    """$ missing from a line in months it was not billed: the median of its non-gap neighbours (+-3 months) minus what was billed.
    months: the gap months to fill (a reviewed rule). None = detect them (a month under half its neighbours' median)."""
    idx = list(v.index)
    gap = pd.Series(0.0, index=v.index)
    cand = set(months) if months is not None else set(idx)
    for i, d in enumerate(idx):
        if d not in cand:
            continue
        nb = [idx[j] for j in range(max(0, i - window), min(len(idx), i + window + 1)) if j != i and (months is None or idx[j] not in cand)]
        if len(nb) < 2:
            continue
        typical = float(v.reindex(nb).median())
        if months is not None:
            gap[d] = max(typical - v[d], 0.0)
        elif typical > 0 and v[d] < 0.5 * typical:
            gap[d] = typical - v[d]
    return gap


def _parse_months(s: str) -> list[pd.Timestamp]:
    return [pd.Timestamp(x.strip() + "-01") for x in str(s).split(";") if x.strip()]


def add_backs(df: pd.DataFrame, rules: pd.DataFrame | None, idx: pd.DatetimeIndex) -> tuple[list, dict]:
    """Billing gaps to add back before measuring savings, so a missing bill is never mistaken for a saving.
    Returns ([(provider, account, service, gap Series)] from the reviewed rules, {provider: (SP gap Series, SP share by account)})."""
    from .nextyear import SP_NAME, load_rules
    rules = load_rules() if rules is None else rules
    out = []
    for r in rules[rules["action"].str.strip().str.lower() == "add_back"].itertuples():
        v = df[(df.provider == r.provider) & (df.account == r.account) & (df.service == r.service)].groupby("date").cost_net.sum().reindex(idx).fillna(0.0)
        g = local_gap(v, _parse_months(r.months))
        if g.sum() > 0:
            out.append((r.provider, r.account, r.service, g))
    sp = {}
    for p in df.provider.unique():
        d = df[(df.provider == p) & (df.service == SP_NAME)]
        if d.empty:
            continue
        v = d.groupby("date").cost_net.sum().reindex(idx).fillna(0.0)
        g = local_gap(v, None)
        if g.sum() > 0:
            share = d.groupby("account").cost_net.sum()
            sp[p] = (g, share / share.sum() if share.sum() > 0 else share)
    return out, sp


def adjusted_series(df: pd.DataFrame, level: str, rules: pd.DataFrame | None = None) -> pd.DataFrame:
    """series() with reviewed billing gaps and Savings Plans fee-timing gaps added back."""
    S = series(df, level)
    gaps, sp = add_backs(df, rules, S.index)
    code_of = dim_of(df, level) if level in CROSS else {}
    for p, a, sv, g in gaps:
        col = (ALL, code_of.get((p, a))) if level in CROSS else (p, a if level == "account" else sv)
        if col in S.columns:
            S[col] = S[col] + g
    if level == "account" or level in CROSS:
        for p, (g, share) in sp.items():
            for acc, sh in share.items():
                col = (ALL, code_of.get((p, acc))) if level in CROSS else (p, acc)
                if col in S.columns:
                    S[col] = S[col] + g * sh
    return S


def adjusted_totals(df: pd.DataFrame, rules: pd.DataFrame | None = None) -> pd.DataFrame:
    T = total_series(df)
    gaps, sp = add_backs(df, rules, T.index)
    for p, a, sv, g in gaps:
        if (p, "All") in T.columns:
            T[(p, "All")] = T[(p, "All")] + g
    for p, (g, share) in sp.items():
        if (p, "All") in T.columns:
            T[(p, "All")] = T[(p, "All")] + g
    return T


def total_series(df: pd.DataFrame) -> pd.DataFrame:
    p = df.pivot_table(index="date", columns="provider", values="cost_net", aggfunc="sum")
    p = p.reindex(pd.date_range(df["date"].min(), df["date"].max(), freq="MS")).fillna(0.0)
    p.columns = pd.MultiIndex.from_product([p.columns, ["All"]])
    return p


# --------------------------------------------------------------------------------------- the scan
@dataclass
class Scan:
    ledger: pd.DataFrame            # one row per line that stepped down
    erosion: pd.DataFrame           # lines that fell and then climbed back
    avoided: pd.DataFrame           # month x class: $ avoided that month versus the old peak level
    materiality: float


def _scan_line(v: np.ndarray, idx: pd.DatetimeIndex, mat: float):
    n = len(v)
    if n < 9:
        return None
    r3 = pd.Series(v).rolling(3).mean().to_numpy()
    cand = r3[: n - 3]                                    # windows ending at least 3 months before the latest month
    if not np.isfinite(cand[2:]).any():
        return None
    i_pk = int(np.nanargmax(np.where(np.isfinite(cand), cand, -np.inf)))
    peak = float(r3[i_pk])
    if peak < mat:
        return None
    window = v[i_pk - 2: i_pk + 1]
    isolated = bool(window.sum() > 0 and window.max() / window.sum() >= 0.7)
    now = float(v[-3:].mean())
    last3 = v[-3:]
    out = {"peak": peak, "now": now, "i_pk": i_pk, "isolated": isolated, "peak_window": f"{idx[i_pk - 2]:%b %Y} – {idx[i_pk]:%b %Y}"}
    # ---- erosion: fell to a trough after the peak and then climbed back
    tr = r3[i_pk + 1: n - 2]
    if len(tr) and np.isfinite(tr).any() and not isolated:
        j = int(np.nanargmin(np.where(np.isfinite(tr), tr, np.inf)))
        trough, i_tr = float(tr[j]), i_pk + 1 + j
        info = dict(trough=trough, trough_end=idx[i_tr], given_back=(now - trough) / (peak - trough) if peak > trough else 0.0)
        out["trough_info"] = info                                  # always kept: the provider summary reports it with a gentler threshold
        if peak - trough >= mat and now - trough >= mat and now >= 1.15 * trough:
            out["erosion"] = info
    # ---- ledger
    drop = peak - now
    if drop >= mat and now <= 0.85 * peak and last3.max() < 0.95 * peak:
        low = last3 < 0.05 * peak
        gap = bool(low.any() and not low.all())
        cls = "One-off" if isolated else ("Check" if gap else ("Ended" if now < 0.1 * peak else "Reduced"))
        mid = now + 0.5 * drop
        above = np.where(v[i_pk:] > mid)[0]
        since_i = min(i_pk + (int(above.max()) if len(above) else 0) + 1, n - 1)
        avoided = peak - v[since_i:]
        out.update(cls=cls, lower=drop, since=idx[since_i], realized=0.0 if cls in ("One-off", "Check") else float(avoided.sum()),
                   avoided=pd.Series(avoided, index=idx[since_i:]) if cls in ("Reduced", "Ended") else None)
    return out


def scan(S: pd.DataFrame, mat: float = MATERIALITY, owners: dict | None = None, level_name: str = "name") -> Scan:
    led, ero, av = [], [], {"Reduced": [], "Ended": []}
    idx = S.index
    for (prov, name), s in S.items():
        if name in SKIP:
            continue
        r = _scan_line(s.to_numpy(dtype=float), idx, mat)
        if not r:
            continue
        own = (owners or {}).get((prov, name), "")
        if "cls" in r:
            led.append(dict(provider=prov, name=name, owner=own, cls=r["cls"], peak=r["peak"], peak_window=r["peak_window"], now=r["now"],
                            lower=r["lower"], annualized=r["lower"] * 12, since=r["since"], realized=r["realized"], pct=r["lower"] / r["peak"]))
            if r.get("avoided") is not None:
                av[r["cls"]].append(r["avoided"])
        if "erosion" in r:
            e = r["erosion"]
            ero.append(dict(provider=prov, name=name, owner=own, peak=r["peak"], peak_window=r["peak_window"], trough=e["trough"],
                            trough_end=e["trough_end"], now=r["now"], regrown=r["now"] - e["trough"], given_back=e["given_back"]))
    ledger = pd.DataFrame(led, columns=["provider", "name", "owner", "cls", "peak", "peak_window", "now", "lower", "annualized", "since", "realized", "pct"])
    ledger = ledger.sort_values("realized", ascending=False).reset_index(drop=True)
    erosion = pd.DataFrame(ero, columns=["provider", "name", "owner", "peak", "peak_window", "trough", "trough_end", "now", "regrown", "given_back"])
    erosion = erosion.sort_values("regrown", ascending=False).reset_index(drop=True)
    avoided = pd.DataFrame(0.0, index=idx, columns=["Reduced", "Ended"])
    for k, lst in av.items():
        for a in lst:
            avoided[k] = avoided[k].add(a.reindex(idx).fillna(0.0), fill_value=0.0)
    return Scan(ledger, erosion, avoided, mat)


def owners_map(df: pd.DataFrame, level: str = "account") -> dict:
    """(provider, account) -> the owner carrying most of its recent spend (blank when unassigned)."""
    if level in CROSS and "owner" in df:
        d = df[df.date > df.date.max() - pd.DateOffset(months=12)]
        g = d.groupby([level, "owner"]).cost_net.sum().reset_index().sort_values("cost_net", ascending=False).drop_duplicates(level)
        return {(ALL, getattr(r, level)): ("" if r.owner == "(unassigned)" else r.owner) for r in g.itertuples()}
    if level != "account" or "owner" not in df:
        return {}
    d = df[df.date > df.date.max() - pd.DateOffset(months=12)]
    g = d.groupby(["provider", "account", "owner"]).cost_net.sum().reset_index().sort_values("cost_net", ascending=False).drop_duplicates(["provider", "account"])
    return {(r.provider, r.account): ("" if r.owner == "(unassigned)" else r.owner) for r in g.itertuples()}


def annotate(sc: Scan, df: pd.DataFrame, level: str) -> Scan:
    """Add project_code / environment / cost_center to the ledger and erosion rows (account lens), or project_code (project-code lens)."""
    out = []
    for t in (sc.ledger, sc.erosion):
        t = t.copy()
        if level == "account":
            for col in ("project_code", "environment", "cost_center", "status"):
                if col not in df:
                    continue
                m = dim_of(df, col)
                t[col] = [m.get((p, n), "") for p, n in zip(t.provider, t.name)]
        elif level in CROSS:
            t[level] = t["name"]
        out.append(t)
    return Scan(out[0], out[1], sc.avoided, sc.materiality)


# --------------------------------------------------------------------------------------- headline numbers and words
def kpis(sc: Scan, h: History) -> list[dict]:
    L = sc.ledger
    red, end = L[L.cls == "Reduced"], L[L.cls == "Ended"]
    giv = sc.erosion.regrown.sum()
    return [
        dict(label="Savings run-rate (reduced lines)", value=money(red.lower.sum()) + "/mo", delta=f"{money(red.annualized.sum())}/yr", neutral=True,
             help="Monthly drop of lines that still run but cost less, versus each line's own earlier peak"),
        dict(label="Realized to date", value=money(red.realized.sum()), delta=f"{len(red)} lines", neutral=True,
             help="What those lines would have cost at their old level, minus what they cost, since each one stepped down"),
        dict(label="Shutdowns (ended lines)", value=money(end.lower.sum()) + "/mo", delta=f"{len(end)} lines", neutral=True,
             help="Lines now near $0. Confirm each was intended before counting it as a saving"),
        dict(label="Savings given back", value=money(giv) + "/mo", delta=f"{len(sc.erosion)} lines", neutral=True, inverse=True,
             help="Lines that dropped and then climbed back up"),
        dict(label="Not counted", value=str(int(L.cls.isin(['One-off', 'Check']).sum())), delta="one-offs / billing gaps", neutral=True,
             help="Spikes that did not repeat, or lines with $0 months that look like billing gaps"),
    ]


def narrative(sc: Scan, h: History, lens: str) -> list[str]:
    L, E = sc.ledger, sc.erosion
    out = []
    red, end = L[L.cls == "Reduced"], L[L.cls == "Ended"]
    if len(red):
        top = red.head(3)
        out.append(f"**{len(red)} {lens}s are running smaller than at their peak**, taking **{money(red.lower.sum())} a month ({money(red.annualized.sum())} a year)** "
                   f"off the run-rate; **{money(red.realized.sum())} has been avoided so far**. Largest: "
                   + "; ".join(f"{r.provider} · {r.name[:34]} ({money(r.realized)}, {r.pct:.0%} lower)" for r in top.itertuples()) + ".")
        byp = red.groupby("provider").lower.sum()
        out.append("By provider: " + ", ".join(f"{p} {money(v)}/mo" for p, v in byp.items()) + ".")
    if len(end):
        conf = int(end["status"].eq("Deleted").sum()) if "status" in end else 0
        out.append(f"**{len(end)} lines ended** ({money(end.lower.sum())}/mo)" + (f", {conf} of them marked Deleted in the owner files" if conf else "")
                   + ": treat as savings only after the owner confirms the shutdown was intended. "
                   f"Biggest: " + "; ".join(f"{r.provider} · {r.name[:30]} ({money(r.lower)}/mo)" for r in end.sort_values('lower', ascending=False).head(3).itertuples()) + ".")
    if len(E):
        out.append(f"**{money(E.regrown.sum())}/mo of earlier reductions has come back** across {len(E)} lines (largest: "
                   + "; ".join(f"{r.provider} · {r.name[:30]} {r.given_back:.0%} given back" for r in E.head(3).itertuples()) + ").")
    ex = L[L.cls.isin(["One-off", "Check"])]
    if len(ex):
        out.append(f"Not counted: {int((ex.cls == 'One-off').sum())} one-off spikes and {int((ex.cls == 'Check').sum())} lines with possible billing gaps "
                   f"({money(ex.peak.sum())}/mo of peak levels that would otherwise look like savings).")
    return out


# --------------------------------------------------------------------------------------- figures
def fig_over_time(sc: Scan) -> go.Figure:
    a = sc.avoided
    fig = go.Figure()
    for k in ("Reduced", "Ended"):
        fig.add_bar(x=a.index, y=a[k], name={"Reduced": "Reduced lines (saving)", "Ended": "Ended lines (confirm)"}[k], marker_color=CLASS_COLOR[k],
                    hovertemplate="%{y:$,.0f}")
    cum = a["Reduced"].cumsum()
    fig.add_scatter(x=a.index, y=cum, name="Cumulative (reduced lines)", mode="lines", yaxis="y2", line=dict(color="#0f172a", width=2.5),
                    hovertemplate="%{y:$,.0f}")
    fig.update_layout(template=TEMPLATE, barmode="stack", height=330, margin=dict(t=30, b=10), hovermode="x unified", legend=dict(orientation="h", y=-0.15),
                      yaxis=dict(tickformat="$.2~s", title="Avoided per month", rangemode="tozero"), yaxis2=dict(overlaying="y", side="right", tickformat="$.2~s", showgrid=False, title="Cumulative", rangemode="tozero"),
                      xaxis=dict(tickformat="%b %y", dtick="M2"))
    return fig


def fig_top(sc: Scan, n: int = 12) -> go.Figure:
    L = sc.ledger[sc.ledger.cls.isin(["Reduced", "Ended"])].sort_values("lower", ascending=False).head(n).iloc[::-1]
    fig = go.Figure(go.Bar(x=L.lower, y=[f"{p} · {nm[:30]}" for p, nm in zip(L.provider, L.name)], orientation="h", marker_color=[CLASS_COLOR[c] for c in L.cls],
                           text=[f"{money(d)}/mo" for d in L.lower], textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}<br>%{x:$,.0f} lower per month<extra></extra>"))
    fig.update_layout(template=TEMPLATE, height=max(260, 26 * len(L) + 80), margin=dict(t=20, b=10, l=10, r=70), xaxis=dict(tickformat="$.2~s"), yaxis=dict(automargin=True))
    return fig


def fig_arc(df: pd.DataFrame) -> go.Figure:
    """Each provider's 3-month average over the whole history: where it peaked, where it fell to, where it is now."""
    T = adjusted_totals(df)
    fig = go.Figure()
    for (p, _), s in T.items():
        r = s.rolling(3).mean()
        fig.add_scatter(x=r.index, y=r, name=p, mode="lines", line=dict(color=COLORS.get(p, "#64748b"), width=2.5), hovertemplate="%{y:$,.0f}")
        i_pk = r.iloc[: len(r) - 3].idxmax()
        i_tr = r.loc[i_pk:].iloc[1:-2].idxmin() if len(r.loc[i_pk:]) > 4 else None
        declined = r.iloc[-1] < 0.95 * r[i_pk]                      # only mark a peak the line has actually come down from
        if declined:
            fig.add_scatter(x=[i_pk], y=[r[i_pk]], mode="markers+text", text=[f"peak {money(r[i_pk])}"], textposition="top center", showlegend=False,
                            marker=dict(size=9, color=COLORS.get(p, "#64748b")))
        if declined and i_tr is not None and r[i_tr] < r[i_pk]:
            fig.add_scatter(x=[i_tr], y=[r[i_tr]], mode="markers+text", text=[f"low {money(r[i_tr])}"], textposition="bottom center", showlegend=False,
                            marker=dict(size=9, symbol="diamond", color=COLORS.get(p, "#64748b")))
        fig.add_scatter(x=[r.index[-1]], y=[r.iloc[-1]], mode="markers+text", text=[money(r.iloc[-1])], textposition="middle right", showlegend=False,
                        marker=dict(size=9, color="#0f172a"))
    fig.update_layout(template=TEMPLATE, height=320, margin=dict(t=20, b=10, r=50), hovermode="x unified", legend=dict(orientation="h", y=-0.15),
                      yaxis=dict(tickformat="$.2~s", rangemode="tozero", title="3-month average / month"), xaxis=dict(tickformat="%b %y"))
    return fig


# --------------------------------------------------------------------------------------- tables
def _dim_cols(out: pd.DataFrame, L: pd.DataFrame) -> pd.DataFrame:
    for col, label in (("project_code", "Project code"), ("environment", "Environment"), ("cost_center", "Cost center"), ("status", "Status")):
        if col in L and not (col == "project_code" and "name" in L and (L[col] == L["name"]).all()):
            out[label] = L[col].values
    return out


def ledger_display(L: pd.DataFrame) -> pd.DataFrame:
    if L.empty:
        return L
    out = pd.DataFrame({"Provider": L.provider, "Line": L.name.str.slice(0, 46), "Owner": L.owner})
    out = _dim_cols(out, L)
    return out.assign(**{"Type": L.cls, "Peak /mo": L.peak.map(money),
                         "Peak window": L.peak_window, "Now /mo": L.now.map(money), "Lower by /mo": L["lower"].map(money), "%": L.pct.map(lambda v: f"-{v:.0%}"),
                         "Since": L.since.dt.strftime("%b %Y"), "Realized to date": L.realized.map(money),
                         "Per year": [money(a) if c in ("Reduced", "Ended") else "–" for a, c in zip(L.annualized, L.cls)]})


def erosion_display(E: pd.DataFrame) -> pd.DataFrame:
    if E.empty:
        return E
    return pd.DataFrame({"Provider": E.provider, "Line": E.name.str.slice(0, 46), "Owner": E.owner, "Peak /mo": E.peak.map(money), "Peak window": E.peak_window,
                         "Low /mo": E.trough.map(money), "Low reached": E.trough_end.dt.strftime("%b %Y"), "Now /mo": E.now.map(money),
                         "Came back /mo": E.regrown.map(money), "Share given back": E.given_back.map(lambda v: f"{min(v, 9.99):.0%}" + (" (above old peak)" if v > 1 else ""))})


# --------------------------------------------------------------------------------------- owner scorecard
BY_LABEL = {"owner": "Owner", "project_code": "Project code", "cost_center": "Cost center"}
BY_MISSING = {"owner": "(unassigned)", "project_code": "(no project code)", "cost_center": "(no cost center)"}


def owner_scorecard(df: pd.DataFrame, h: History, acct: Scan, by: str = "owner") -> pd.DataFrame:
    """One row per owner (or project code / cost center): spend, change vs the 12 months before, run-rate, savings and what is rising or falling.
    acct = the account-lens scan, annotated (savings are credited to the group each account belongs to)."""
    d = df
    cur, prev = d[d.date.isin(h.cur)], d[d.date.isin(h.prev)] if h.has_prev else d.iloc[0:0]
    c, p = cur.groupby(by).cost_net.sum(), prev.groupby(by).cost_net.sum()
    last3 = d[d.date.isin(h.m.index[-3:])].groupby(by).cost_net.sum() / 3
    ly3 = d[d.date.isin(h.m.index[-3:] - pd.DateOffset(years=1))].groupby(by).cost_net.sum() / 3
    t = pd.DataFrame({"latest": c, "prior": p, "exit": last3, "exit_ly": ly3}).fillna(0.0)
    t["change"] = t.latest - t.prior
    t["pct"] = t.change / t.prior.where(t.prior > 1000)
    t["share"] = t.latest / t.latest.sum()
    t["accounts"] = cur[cur.cost_net > 0].groupby(by).account.nunique()
    L, E = acct.ledger, acct.erosion
    if by not in L:
        L, E = L.assign(**{by: ""}), E.assign(**{by: ""})
    red = L[L.cls == "Reduced"].groupby(by)
    t["saving_mo"] = red["lower"].sum()
    t["realized"] = red["realized"].sum()
    t["ended_mo"] = L[L.cls == "Ended"].groupby(by)["lower"].sum()
    t["given_back_mo"] = E.groupby(by).regrown.sum()
    # biggest rising / falling account per group (latest 12 vs prior 12)
    a = pd.DataFrame({"latest": cur.groupby([by, "provider", "account"]).cost_net.sum(),
                      "prior": prev.groupby([by, "provider", "account"]).cost_net.sum()}).fillna(0.0)
    a["chg"] = a.latest - a.prior
    up, dn = {}, {}
    for o, g in a.groupby(level=0):
        g = g.droplevel(0)
        if (g.chg > 0).any():
            (pv, ac), r = g.chg.idxmax(), g.loc[g.chg.idxmax()]
            up[o] = f"{pv} · {ac[:28]} ({'+' + money(r.chg)})"
        if (g.chg < 0).any():
            (pv, ac), r = g.chg.idxmin(), g.loc[g.chg.idxmin()]
            dn[o] = f"{pv} · {ac[:28]} ({money(r.chg)})"
    t["biggest_rise"], t["biggest_fall"] = pd.Series(up), pd.Series(dn)
    t = t[(t.latest.abs() + t.prior.abs()) > 0]
    t[["saving_mo", "realized", "ended_mo", "given_back_mo"]] = t[["saving_mo", "realized", "ended_mo", "given_back_mo"]].fillna(0.0)
    t.index.name = by
    return t.sort_values("latest", ascending=False)


def scorecard_display(t: pd.DataFrame) -> pd.DataFrame:
    sg = lambda v: ("+" if v > 0 else "") + money(v)
    return pd.DataFrame({BY_LABEL.get(t.index.name, "Owner"): t.index, "Accounts / projects": t.accounts.fillna(0).astype(int).values, "Latest 12 mo": t.latest.map(money).values,
                         "Share": t.share.map(lambda v: f"{v:.0%}").values, "Prior 12 mo": t.prior.map(money).values, "Change": t.change.map(sg).values,
                         "%": t.pct.map(lambda v: "–" if pd.isna(v) else (f"{1 + v:.0f}×" if v >= 3 else f"{v:+.0%}")).values,
                         "Exit run-rate /mo": t.exit.map(money).values, "Savings /mo": t.saving_mo.map(money).values, "Realized": t.realized.map(money).values,
                         "Given back /mo": t.given_back_mo.map(money).values, "Biggest rise": t.biggest_rise.fillna("").values,
                         "Biggest fall": t.biggest_fall.fillna("").values})


def fig_owner_change(t: pd.DataFrame, n: int = 14) -> go.Figure:
    t = t.reindex(t.change.abs().sort_values(ascending=False).index).head(n).iloc[::-1]
    fig = go.Figure(go.Bar(x=t.change, y=t.index, orientation="h", marker_color=["#b91c1c" if v > 0 else "#15803d" for v in t.change],
                           text=[("+" if v > 0 else "") + money(v) for v in t.change], textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}<br>%{x:$,.0f} vs prior 12 months<extra></extra>"))
    lo, hi = min(t.change.min(), 0), max(t.change.max(), 0)
    fig.update_layout(template=TEMPLATE, height=max(280, 28 * len(t) + 80), margin=dict(t=20, b=10, l=10, r=70),
                      xaxis=dict(tickformat="$.2~s", zeroline=True, range=[lo * 1.3 if lo < 0 else 0, hi * 1.2 if hi > 0 else 0]),
                      yaxis=dict(automargin=True))
    return fig


def owner_detail(df: pd.DataFrame, h: History, owner: str, acct: Scan, top: int = 8, by: str = "owner") -> dict:
    """Everything for one owner's (or project code's / cost center's) review: monthly chart, account table, savings, erosion, rising lines, finance notes."""
    d = df[df[by] == owner]
    cur, prev = d[d.date.isin(h.cur)], d[d.date.isin(h.prev)]
    a = pd.DataFrame({"latest": cur.groupby(["provider", "account"]).cost_net.sum(), "prior": prev.groupby(["provider", "account"]).cost_net.sum()}).fillna(0.0)
    a["change"] = a.latest - a.prior
    a["pct"] = a.change / a.prior.where(a.prior > 1000)
    a = a.sort_values("latest", ascending=False).reset_index().rename(columns={"account": "name"})
    L = acct.ledger[acct.ledger[by] == owner] if by in acct.ledger else acct.ledger.iloc[0:0]
    E = acct.erosion[acct.erosion[by] == owner] if by in acct.erosion else acct.erosion.iloc[0:0]
    notes = list(dict.fromkeys(x for x in cur["finance_notes"] if x)) if "finance_notes" in cur else []
    top_names = a.head(top)
    piv = d.assign(line=[f"{p} · {n[:30]}" for p, n in zip(d.provider, d.account)]).pivot_table(index="date", columns="line", values="cost_net", aggfunc="sum")
    piv = piv.reindex(h.m.index).fillna(0.0)
    keep = piv.sum().nlargest(top).index
    other = piv.drop(columns=keep).sum(axis=1)
    fig = go.Figure()
    for col in keep:
        fig.add_bar(x=piv.index, y=piv[col], name=col, hovertemplate="%{y:$,.0f}")
    if other.sum() > 0:
        fig.add_bar(x=piv.index, y=other, name="Other", marker_color="#cbd5e1", hovertemplate="%{y:$,.0f}")
    fig.update_layout(template=TEMPLATE, barmode="stack", height=360, margin=dict(t=20, b=10), hovermode="x unified", legend=dict(font=dict(size=10)),
                      yaxis=dict(tickformat="$.2~s"), xaxis=dict(tickformat="%b %y", dtick="M2"))
    return dict(accounts=a, ledger=L, erosion=E, fig=fig, latest=a.latest.sum(), prior=a.prior.sum(), notes=notes)


def landed(ledger: pd.DataFrame, by: str) -> pd.DataFrame:
    """Where the (reduced-line) savings landed: run-rate and realized to date per project code / environment / cost center. Needs an annotated ledger."""
    if by not in ledger:
        return pd.DataFrame(columns=["lower", "realized", "lines"])
    r = ledger[ledger.cls == "Reduced"]
    g = r.groupby(by).agg(lower=("lower", "sum"), realized=("realized", "sum"), lines=("name", "size"))
    return g.sort_values("realized", ascending=False)


def fig_landed(t: pd.DataFrame, title: str, n: int = 10) -> go.Figure:
    d = t.head(n).iloc[::-1]
    fig = go.Figure(go.Bar(x=d.realized, y=d.index.astype(str), orientation="h", marker_color=CLASS_COLOR["Reduced"], cliponaxis=False,
                           text=[f"{money(r)}  ({money(m)}/mo)" for r, m in zip(d.realized, d["lower"])], textposition="outside",
                           hovertemplate="%{y}<br>%{x:$,.0f} realized to date<extra></extra>"))
    fig.update_layout(template=TEMPLATE, title=title, height=max(240, 26 * len(d) + 90), margin=dict(t=40, b=10, l=10, r=120), xaxis=dict(tickformat="$.2~s"), yaxis=dict(automargin=True))
    fig.update_xaxes(range=[0, max(d.realized.max(), 1) * 1.45] if len(d) else None)
    return fig


def arc_lines(df: pd.DataFrame) -> list[str]:
    """One sentence per provider: how far total spend fell from its peak, where it bottomed out and how much has come back.
    Uses totals with billing gaps added back, so a missing bill is not read as a low point."""
    T = adjusted_totals(df)
    out = []
    for (p, _), s_ in T.items():
        r = _scan_line(s_.to_numpy(dtype=float), T.index, MATERIALITY)
        if not r:
            continue
        t = r.get("trough_info")
        if t and r["peak"] - t["trough"] >= MATERIALITY and r["now"] > 1.02 * t["trough"] and t["trough"] < 0.95 * r["peak"]:
            out.append(f"**{p}** averaged {money(r['peak'])}/mo at its peak ({r['peak_window']}), fell to {money(t['trough'])}/mo by {t['trough_end']:%b %Y} "
                       f"({1 - t['trough'] / r['peak']:.0%} lower) and is now {money(r['now'])}/mo, so **{min(t['given_back'], 9.99):.0%} of the reduction has come back**.")
        elif r["now"] >= 0.95 * r["peak"]:
            out.append(f"**{p}** is at {money(r['now'])}/mo, at or near the highest level in the history: no reduction to report at the provider level.")
        else:
            out.append(f"**{p}** averaged {money(r['peak'])}/mo at its peak ({r['peak_window']}) and is now {money(r['now'])}/mo ({1 - r['now'] / r['peak']:.0%} lower).")
    return out
