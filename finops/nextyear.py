"""Next-year (budget) forecast. Ported from your cloud_forecast.py, forecast_detail.py and AWS_FinOps_By_Account.py.

Method (kept simple enough to explain to Finance):
  Cloud Services, per provider: start at the exit run-rate (last 3 billed months) + Theil-Sen trend (median of all
  pairwise slopes, robust to one-off months). Scenarios: Low = flat, Base = damped trend (momentum fades 15%/month),
  High = trend continues linearly. AWS Savings-Plan-fee gaps are added back for the TREND ONLY.
  Per account / project: each line competes Flat / Damped / Linear / Holt(ETS) on a backtest; simplest model within
  5% of the best MAE wins; forecasts are capped (2x) and floored (0.5x) vs the exit run-rate.
  Marketplace: contract renewal calendar (see finops/marketplace.py).
Basis = the last 12 billed months. Earlier months (the dashboard holds ~2 years) are drawn as context and used by the look-back test
(finops/history.py: lookback), not by the model itself.
"""
from __future__ import annotations
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from . import marketplace as mkt
from .config import load_config, resolve
from .schema import CLOUD, MARKETPLACE, TAX

warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"statsmodels\.")
warnings.filterwarnings("ignore", category=UserWarning, module=r"statsmodels\.")

DAMPING = 0.85
MODELS = ["Flat", "Damped trend", "Linear", "Holt (ETS)"]      # simplest first (tie-break order)
TIE_TOLERANCE, GROWTH_CAP, DECLINE_FLOOR = 0.05, 2.0, 0.5
BASIS_MONTHS = 12
SCEN = ["Low", "Base", "High"]
SP_NAME = "Savings Plans for AWS Compute usage"
PROVIDERS = ["AWS", "GCP"]


def theil_sen(y) -> float:
    v = np.asarray(y, dtype=float)
    slopes = [(v[j] - v[i]) / (j - i) for i in range(len(v)) for j in range(i + 1, len(v))]
    return float(np.median(slopes)) if slopes else 0.0


# ------------------------------------------------------------------------------------------ line models
def _predict(model: str, y: np.ndarray, h: int) -> np.ndarray:
    steps = np.arange(1, h + 1)
    anchor = y[-3:].mean() if len(y) >= 3 else y.mean()
    if model == "Flat":
        return np.full(h, anchor)
    slope = theil_sen(y)
    if model == "Damped trend":
        return anchor + slope * np.cumsum(DAMPING ** steps)
    if model == "Linear":
        return anchor + slope * steps
    if model == "Holt (ETS)":
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore")
            fit = ExponentialSmoothing(y.astype(float), trend="add", damped_trend=True, initialization_method="estimated").fit()
        out = np.asarray(fit.forecast(h), dtype=float)
        if not np.isfinite(out).all():
            raise ValueError("non-finite ETS forecast")
        return out
    raise ValueError(model)


def backtest(y: np.ndarray, model: str, min_train: int = 5, max_h: int = 3) -> np.ndarray:
    errs = []
    for t in range(min_train, len(y)):
        h = min(max_h, len(y) - t)
        errs.extend(_predict(model, y[:t], h) - y[t:t + h])
    return np.array(errs)


@dataclass
class LineForecast:
    name: str
    actual: pd.Series
    forecast: pd.Series
    lo: pd.Series
    hi: pd.Series
    model: str
    mae: float
    mape: float
    capped: bool = False
    floored: bool = False

    @property
    def confidence(self) -> str:
        e = self.mape
        return "Low" if not np.isfinite(e) or e >= 0.25 else ("Medium" if e >= 0.10 else "High")


def forecast_line(name: str, actual: pd.Series, horizon: pd.DatetimeIndex, force_model: str | None = None) -> LineForecast:
    y = actual.to_numpy(dtype=float)
    scores = {}
    for m in MODELS:
        try:
            e = backtest(y, m)
            if len(e):
                scores[m] = dict(mae=float(np.mean(np.abs(e))), rmse=float(np.sqrt(np.mean(e ** 2))))
        except Exception:
            continue
    if not scores:
        scores = {"Flat": dict(mae=np.nan, rmse=0.0)}
    best = np.nanmin([v["mae"] for v in scores.values()]) if not all(np.isnan(v["mae"]) for v in scores.values()) else 0
    model = force_model or next((m for m in MODELS if m in scores and (np.isnan(best) or scores[m]["mae"] <= best * (1 + TIE_TOLERANCE) + 1e-9)), "Flat")
    if model not in scores:
        scores[model] = scores.get("Damped trend", next(iter(scores.values())))
    pred = _predict(model, y, len(horizon))
    anchor = y[-3:].mean()
    capped = floored = False
    if anchor > 0 and pred.max() > GROWTH_CAP * anchor:
        pred, capped = np.minimum(pred, GROWTH_CAP * anchor), True
    if anchor > 0 and pred.min() < DECLINE_FLOOR * anchor:
        pred, floored = np.maximum(pred, DECLINE_FLOOR * anchor), True
    pred = np.clip(pred, 0, None)
    rmse = scores[model]["rmse"]
    width = 1.28 * (0 if np.isnan(rmse) else rmse) * np.sqrt(np.arange(1, len(horizon) + 1))
    return LineForecast(name, actual, pd.Series(pred, horizon), pd.Series(np.clip(pred - width, 0, None), horizon),
                        pd.Series(pred + width, horizon), model, scores[model]["mae"],
                        scores[model]["mae"] / anchor if anchor else np.nan, capped, floored)


def topdown(s: pd.Series, gap: pd.Series, horizon: pd.DatetimeIndex, trend_from: pd.Timestamp | None = None):
    """Low / Base / High from a monthly series (gap = normalization added for the trend only).
    trend_from: measure the trend only from this month onward (the level still uses the latest months)."""
    tr = s + gap
    if trend_from is not None and (tr.index >= trend_from).sum() >= 4:
        tr = tr[tr.index >= trend_from]
    anchor, slope, raw = s.tail(3).mean(), theil_sen(tr), theil_sen(s)
    h = np.arange(1, len(horizon) + 1)
    flat, damped, linear = np.full(len(h), anchor), anchor + slope * np.cumsum(DAMPING ** h), anchor + slope * h
    out = pd.DataFrame({"Low": np.minimum.reduce([flat, damped, linear]), "Base": damped,
                        "High": np.maximum.reduce([flat, damped, linear])}, index=horizon).clip(lower=0)
    return out, anchor, slope, raw


# ------------------------------------------------------------------------------------------ build
@dataclass
class NextYear:
    fy: int
    last: pd.Timestamp
    basis: pd.DatetimeIndex
    horizon: pd.DatetimeIndex
    svc_actual: dict = field(default_factory=dict)      # provider -> Series (basis months)
    hist: dict = field(default_factory=dict)            # provider -> Series of ALL billed Cloud Services months (context: earlier than the basis)
    svc: dict = field(default_factory=dict)             # provider -> DataFrame[Low, Base, High] over horizon
    params: dict = field(default_factory=dict)
    normalization: dict = field(default_factory=dict)
    drivers: dict = field(default_factory=dict)
    mkt_actual: pd.DataFrame = None                     # basis x provider
    mkt: pd.DataFrame = None                            # horizon x provider
    mkt_detail: pd.DataFrame = None                     # horizon x (provider, contract)
    contracts: pd.DataFrame = None
    tax_rate: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)           # provider -> list[LineForecast] (accounts/projects)
    tables: dict = field(default_factory=dict)          # provider -> accounts table
    anomalies: pd.DataFrame = None
    current_year: dict = field(default_factory=dict)    # provider -> current-year estimate (actual + base)
    rules: pd.DataFrame = None                          # reviewed anomaly rules that were applied
    rule_gaps: dict = field(default_factory=dict)       # provider -> Series of add-backs from the rules (trend only)
    effects: dict = field(default_factory=dict)         # provider -> {raw, adjusted, add_back, trend_since}
    adjusted: bool = True

    @property
    def months(self) -> pd.DatetimeIndex:
        return self.horizon[self.horizon.year == self.fy]


def _short(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


RULE_COLS = ["provider", "account", "service", "months", "action", "note"]


def load_rules() -> pd.DataFrame:
    p = resolve(load_config().get("forecast", {}).get("adjustments_csv", "data/anomaly_rules.csv"))
    if not p.is_file():
        return pd.DataFrame(columns=RULE_COLS)
    r = pd.read_csv(p, dtype=str).fillna("")
    return r.reindex(columns=RULE_COLS, fill_value="")


def _rule_gap(rows: pd.DataFrame, rule, basis: pd.DatetimeIndex) -> pd.Series:
    """Add-back for one reviewed line: typical month minus what was billed, in the listed months only."""
    m = (rows[(rows.provider == rule.provider) & (rows.account == rule.account) & (rows.service == rule.service)]
         .groupby("date")["cost_net"].sum().reindex(basis).fillna(0.0))
    months = [pd.Timestamp(x.strip() + "-01") for x in str(rule.months).split(";") if x.strip()]
    ok = m[~m.index.isin(months)]
    typical = ok[ok > 0.5 * ok.median()].median() if (ok > 0).any() else 0.0
    gap = pd.Series(0.0, index=basis)
    for mo in months:
        if mo in gap.index:
            gap[mo] = max(typical - m[mo], 0.0)
    return gap


def build(df: pd.DataFrame, top_n: int = 12, apply_adjustments: bool = True) -> NextYear:
    """df: billed rows in the normalized schema (all categories). Cached by the page.
    apply_adjustments: use the reviewed anomaly rules (data/anomaly_rules.csv) and [forecast.trend_since]."""
    df = df[~df["is_partial"].astype(bool)]
    last = df["date"].max()
    basis = pd.date_range(last - relativedelta(months=BASIS_MONTHS - 1), last, freq="MS")
    fy = last.year + 1
    horizon = pd.date_range(last + relativedelta(months=1), f"{fy}-12-01", freq="MS")
    ny = NextYear(fy, last, basis, horizon)
    d = df[df["date"].isin(basis)]
    svc_rows = d[d["category"] == CLOUD]
    rules = load_rules() if apply_adjustments else pd.DataFrame(columns=RULE_COLS)
    rules_add = rules[rules["action"].str.strip().str.lower() == "add_back"]
    since_cfg = load_config().get("forecast", {}).get("trend_since", {}) if apply_adjustments else {}
    ny.rules, ny.adjusted = rules, apply_adjustments
    rule_lines = [(r, _rule_gap(svc_rows, r, basis)) for r in rules_add.itertuples()]

    for p in PROVIDERS:
        s = svc_rows[svc_rows["provider"] == p].groupby("date")["cost_net"].sum().reindex(basis).fillna(0.0)
        sp = svc_rows[(svc_rows.provider == p) & (svc_rows.service == SP_NAME)].groupby("date")["cost_net"].sum().reindex(basis).fillna(0.0)
        typical = sp[sp > 0].median() if (sp > 0).any() else 0.0
        gap = (typical - sp).where(sp < 0.5 * typical, 0.0) if typical else pd.Series(0.0, index=basis)
        ny.normalization[p] = gap[gap > 0]
        ny.svc_actual[p] = s
        ny.hist[p] = df[(df["category"] == CLOUD) & (df["provider"] == p)].groupby("date")["cost_net"].sum().sort_index()
        rg = sum((g for r, g in rule_lines if r.provider == p), pd.Series(0.0, index=basis))
        ny.rule_gaps[p] = rg
        since = pd.Timestamp(since_cfg[p] + "-01") if since_cfg.get(p) else None
        raw_df, _, _, _ = topdown(s, gap, horizon)                       # as it was before any review
        ny.svc[p], anchor, slope, raw = topdown(s, gap + rg, horizon, since)
        ny.effects[p] = dict(raw={k: raw_df.loc[raw_df.index.year == fy, k].sum() for k in ("Low", "Base", "High")},
                             adjusted={k: ny.svc[p].loc[ny.svc[p].index.year == fy, k].sum() for k in ("Low", "Base", "High")},
                             add_back=float(rg.sum()), n_rules=sum(1 for r, _ in rule_lines if r.provider == p),
                             trend_since=since, slope_raw=theil_sen(s + gap), slope_adj=slope)
        ny.params[p] = dict(ytd=s.sum(), months=len(s), anchor=anchor, slope=slope, slope_pct=slope / anchor if anchor else 0.0,
                            first_half=s.iloc[: len(s) // 2].mean(), peak=s.max(), peak_month=s.idxmax(),
                            low=s.min(), low_month=s.idxmin(), raw_slope=raw)
        # growth drivers
        sv = svc_rows[svc_rows.provider == p].pivot_table(index="date", columns="service", values="cost_net", aggfunc="sum").reindex(basis).fillna(0.0)
        sv = sv.loc[:, (sv.mean() > 2000) & (sv.columns != SP_NAME)]
        drv = pd.DataFrame({"Avg / mo": sv.mean(), "Exit run-rate": sv.tail(3).mean(), "Trend $/mo per month": sv.apply(theil_sen)})
        drv["Share of growth"] = drv["Trend $/mo per month"] / slope if slope else np.nan
        ny.drivers[p] = drv.sort_values("Trend $/mo per month", ascending=False)
        cur_year_months = pd.date_range(f"{last.year}-01-01", f"{last.year}-12-01", freq="MS")
        billed_cy = svc_rows[(svc_rows.provider == p) & (svc_rows.date.dt.year == last.year)].cost_net.sum()
        ny.current_year[p] = billed_cy + ny.svc[p].loc[ny.svc[p].index.year == last.year, "Base"].sum()

    # Marketplace calendar
    m_all = df[df["category"] == MARKETPLACE]
    if len(m_all):
        ct, m, _ = mkt.build(m_all, pd.Timestamp(fy, 12, 1))
        ny.contracts = ct
        piv = m.T.groupby(level=0).sum().T
        ny.mkt_actual = piv.reindex(basis).reindex(columns=PROVIDERS, fill_value=0.0).fillna(0.0)
        ny.mkt = piv.reindex(horizon).reindex(columns=PROVIDERS, fill_value=0.0).fillna(0.0)
        ny.mkt_detail = m.reindex(horizon).fillna(0.0)
    else:
        ny.mkt_actual = pd.DataFrame(0.0, index=basis, columns=PROVIDERS)
        ny.mkt = pd.DataFrame(0.0, index=horizon, columns=PROVIDERS)
        ny.mkt_detail = pd.DataFrame(index=horizon)
        ny.contracts = pd.DataFrame()

    tax = d[d["category"] == TAX].groupby("provider")["cost_net"].sum()
    for p in PROVIDERS:
        base = ny.svc_actual[p].sum() + ny.mkt_actual[p].sum()
        ny.tax_rate[p] = float(tax.get(p, 0.0) / base) if base else 0.0

    # per account / project lines (bottom-up), Savings-Plan add-back placed on the paying account
    for p in PROVIDERS:
        rows = svc_rows[svc_rows.provider == p].copy()
        if p == "GCP":
            rows["account"] = rows["account"].replace({"(no project)": "Google Support (no project)"})
        acc = rows.pivot_table(index="date", columns="account", values="cost_net", aggfunc="sum").reindex(basis).fillna(0.0)
        gap = ny.normalization[p].reindex(basis).fillna(0.0)
        if gap.sum() > 0:
            sp_acc = rows[rows.service == SP_NAME].groupby("account")["cost_net"].sum()
            if len(sp_acc) and sp_acc.sum() > 0:
                for a, share in (sp_acc / sp_acc.sum()).items():
                    acc[a] = acc[a] + gap * share
        for r, g in rule_lines:                                    # reviewed add-backs go on the account that carries the line
            if r.provider == p and r.account in acc.columns:
                acc[r.account] = acc[r.account] + g
        order = acc.sum().sort_values(ascending=False).index
        top, rest = list(order[:top_n]), list(order[top_n:])
        lines = [forecast_line(a, acc[a], horizon) for a in top if acc[a].sum() > 0]
        if rest:
            lines.append(forecast_line(f"All other ({len(rest)})", acc[rest].sum(axis=1), horizon, force_model="Damped trend"))
        ny.lines[p] = lines
        ytd_col = "Last 12 mo"
        recs = []
        for lf in lines:
            exit_rr = lf.actual.tail(3).mean()
            fyv = lf.forecast.loc[ny.months].sum()
            recs.append({"Account / Project": lf.name, ytd_col: lf.actual.sum(), "Exit run-rate /mo": exit_rr,
                         f"FY{fy} forecast": fyv, "Low": lf.lo.loc[ny.months].sum(), "High": lf.hi.loc[ny.months].sum(),
                         "Δ vs exit × 12": fyv - exit_rr * 12,
                         "Model": lf.model + (" (capped)" if lf.capped else "") + (" (floored)" if lf.floored else ""),
                         "Confidence": f"{lf.confidence} (±{lf.mape:.0%})" if np.isfinite(lf.mape) else lf.confidence})
        t = pd.DataFrame(recs).set_index("Account / Project")
        if len(t):
            t.insert(1, "Share", t[ytd_col] / t[ytd_col].sum())
        ny.tables[p] = t

    ny.anomalies = find_anomalies(svc_rows, basis, rules)
    return ny


def _anomaly_type(low_mask: np.ndarray) -> str:
    idx = np.where(low_mask)[0]
    run = len(idx) == 1 or bool(np.all(np.diff(idx) == 1))
    if idx[0] == 0 and run:
        return "New workload"
    if idx[-1] == len(low_mask) - 1 and run:
        return "Shutdown / fading"
    return "Gap"


def find_anomalies(rows: pd.DataFrame, basis: pd.DatetimeIndex, rules: pd.DataFrame | None = None, min_typical: float = 3000) -> pd.DataFrame:
    """Account x service lines that normally cost >= min_typical/month but drop under 10% of that in some months,
    classified as Gap / New workload / Shutdown and matched against the reviewed rules."""
    rules = rules if rules is not None else pd.DataFrame(columns=RULE_COLS)
    x = rows.pivot_table(index="date", columns=["provider", "account", "service"], values="cost_net", aggfunc="sum").reindex(basis).fillna(0.0)
    out = []
    for (p, a, sv), ser in x.items():
        typical = ser.median()
        if typical < min_typical:
            continue
        low_mask = (ser < 0.1 * typical).to_numpy()
        if not low_mask.any():
            continue
        kind = _anomaly_type(low_mask)
        hit = rules[(rules.provider == p) & (rules.account == a) & (rules.service == sv)]
        if sv == SP_NAME:
            status = "Added back (automatic)"
        elif len(hit):
            act = hit.iloc[0]["action"].strip().lower()
            status = {"add_back": "Added back (reviewed)", "confirmed_real": "Confirmed real", "pending": "Pending review"}.get(act, "Pending review")
        elif kind == "New workload":
            status = "Real: new workload"
        else:
            status = "Pending review"
        low = ser[low_mask]
        out.append({"Provider": p, "Account / Project": a, "Service": _short(sv, 36), "Type": kind, "Status": status, "Typical /mo": typical,
                    "Months near $0": ", ".join(low.index.strftime("%b %y")), "Missing vs typical": (typical - low).sum()})
    return pd.DataFrame(out).sort_values("Missing vs typical", ascending=False).reset_index(drop=True) if out else pd.DataFrame()


def account_services(df: pd.DataFrame, ny: NextYear, provider: str, account: str, n: int = 8) -> pd.DataFrame:
    """Top services inside one account/project with their own model + FY forecast."""
    rows = df[(~df["is_partial"].astype(bool)) & (df["category"] == CLOUD) & (df["provider"] == provider) & df["date"].isin(ny.basis)]
    if provider == "GCP":
        rows = rows.assign(account=rows["account"].replace({"(no project)": "Google Support (no project)"}))
    sv = rows[rows.account == account].pivot_table(index="date", columns="service", values="cost_net", aggfunc="sum").reindex(ny.basis).fillna(0.0)
    if sv.empty:
        return pd.DataFrame()
    sv = sv[sv.sum().sort_values(ascending=False).index[:n]]
    recs = []
    for name in sv.columns:
        if sv[name].sum() < 100:
            continue
        lf = forecast_line(name, sv[name], ny.horizon)
        recs.append({"Service": _short(name, 44), "Last 12 mo": sv[name].sum(), "Exit run-rate /mo": sv[name].tail(3).mean(),
                     f"FY{ny.fy} forecast": lf.forecast.loc[ny.months].sum(), "Model": lf.model, "Confidence": lf.confidence})
    return pd.DataFrame(recs).set_index("Service") if recs else pd.DataFrame()
