"""Explainable forecasts. Two methods, auto-picked per series by backtest:

  yoy     same month last year x MEDIAN of recent monthly YoY ratios (robust to one-off spikes)
  runrate trailing 3-month average held flat (wins where the series is still ramping, e.g. GCP)
"""
from __future__ import annotations
import numpy as np
import pandas as pd

METHODS = ("yoy", "runrate")


def default_band() -> float:
    from .config import load_config
    return load_config()["forecast"].get("default_band_pct", 15) / 100


def _yoy_ratio(s: pd.Series, window: int) -> float:
    r = s.iloc[-window:]
    p = s.reindex(r.index - pd.DateOffset(years=1)).values
    ok = ~np.isnan(p) & (p > 0)
    return float(np.clip(np.median(r.values[ok] / p[ok]), 0.6, 1.6)) if ok.any() else 1.0


def project(s: pd.Series, months: list[pd.Timestamp], method: str = "yoy",
            window: int = 6, growth_pct: float = 0.0) -> pd.Series:
    """Project `months` in order; later months may build on earlier projected ones."""
    s = s.sort_index().copy()
    adj = 1 + growth_pct / 100
    out = {}
    for m in months:
        if method == "runrate":
            v = s.iloc[-3:].mean()
        else:
            base = s.get(m - pd.DateOffset(years=1))
            v = s.iloc[-3:].mean() if base is None or np.isnan(base) else base * _yoy_ratio(s, window)
        out[m] = max(v * adj, 0.0)
        s.loc[m] = out[m]
    return pd.Series(out)


def backtest_mape(s: pd.Series, method: str = "yoy", n: int = 8, window: int = 6) -> float:
    s = s.sort_index()
    errs = []
    for m in s.index[-n:]:
        train = s[s.index < m]
        if len(train) < 13 or s[m] <= 0:
            continue
        errs.append(abs(project(train, [m], method, window).iloc[0] - s[m]) / s[m])
    return float(np.mean(errs)) if errs else float("nan")


def pick_method(s: pd.Series, window: int = 6) -> tuple[str, float]:
    """Best method by backtest MAPE. With about two years of history the backtest has up to 8 points; if none is
    possible, use yoy once a full year exists (needed for 'same month last year'), else runrate."""
    if len(s) < 13:
        return "runrate", float("nan")
    scores = {m: backtest_mape(s, m, window=window) for m in METHODS}
    scores = {k: v for k, v in scores.items() if not np.isnan(v)}
    if not scores:
        return "yoy", float("nan")
    best = min(scores, key=scores.get)
    return best, scores[best]


def year_forecast(s: pd.Series, year: int, window: int = 6, growth_pct: float = 0.0,
                  method: str = "auto") -> pd.DataFrame:
    """Each month of `year`: actual (billed) or projected, with low/high band (± backtest MAPE).
    df.attrs holds 'method' and 'mape'."""
    s = s.sort_index()
    s = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="MS"), fill_value=0.0)  # months with no billing = $0
    s = s[s.index >= s.ne(0).idxmax()] if s.ne(0).any() else s       # drop leading zero months
    mape = float("nan")
    if method == "auto":
        method, mape = pick_method(s, window)
    else:
        mape = backtest_mape(s, method, window=window)
    months = [pd.Timestamp(year, m, 1) for m in range(1, 13)]
    todo = [m for m in months if m > s.index.max()]
    proj = project(s, todo, method, window, growth_pct) if todo else pd.Series(dtype=float)
    band = default_band() if np.isnan(mape) else min(max(mape, 0.05), 0.5)
    rows = []
    for m in months:
        if m in s.index:
            rows.append((m, s[m], s[m], s[m], "actual"))
        elif m < s.index.min():                       # before the series existed (new account/project)
            rows.append((m, 0.0, 0.0, 0.0, "actual"))
        else:
            rows.append((m, proj[m], proj[m] * (1 - band), proj[m] * (1 + band), "forecast"))
    out = pd.DataFrame(rows, columns=["date", "value", "low", "high", "kind"])
    out.attrs.update(method=method, mape=mape)
    return out


def combine(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Sum several year_forecast frames (e.g. AWS + GCP) month by month."""
    out = parts[0][["date", "kind"]].copy()
    for c in ("value", "low", "high"):
        out[c] = sum(p[c].values for p in parts)
    # a month is "actual" only if EVERY provider's month is actual
    is_actual = np.logical_and.reduce([p["kind"].eq("actual").values for p in parts])
    out["kind"] = np.where(is_actual, "actual", "forecast")
    out.attrs.update(method=" + ".join(p.attrs.get("method", "?") for p in parts),
                     mape=float(pd.Series([p.attrs.get("mape", np.nan) for p in parts]).mean()))
    return out[["date", "value", "low", "high", "kind"]]


def forecast_two_years(df: pd.DataFrame, year: int, window: int = 6, growth_pct: float = 0.0,
                       method: str = "auto") -> tuple[pd.DataFrame, pd.DataFrame]:
    """(current_year, next_year) forecasts for a frame of cost rows, modelled per provider then summed."""
    cur_parts, nxt_parts = [], []
    for _, g in df.groupby("provider"):
        s = g.groupby("date")["cost_net"].sum().sort_index()
        cur = year_forecast(s, year, window, method=method)
        full = pd.concat([s, cur[cur.kind == "forecast"].set_index("date").value]).sort_index()
        nxt = year_forecast(full, year + 1, window, growth_pct, method=cur.attrs["method"])
        nxt.attrs["mape"] = cur.attrs["mape"]
        cur_parts.append(cur)
        nxt_parts.append(nxt)
    return combine(cur_parts), combine(nxt_parts)


def entity_table(df: pd.DataFrame, level: str, year: int, window: int = 6, growth_pct: float = 0.0,
                 max_entities: int = 40) -> pd.DataFrame:
    """Current-year and next-year forecast for each entity (account/project/service), largest first."""
    last = df["date"].max()
    recent = df[df["date"] > last - pd.DateOffset(months=12)].groupby(level)["cost_net"].sum()
    rows = []
    for ent in recent.nlargest(max_entities).index:
        g = df[df[level] == ent]
        s = g.groupby("date")["cost_net"].sum().reindex(
            pd.date_range(g["date"].min(), last, freq="MS"), fill_value=0.0)
        if (s > 0).sum() < 4:
            continue
        cur = year_forecast(s, year, window)
        full = pd.concat([s, cur[cur.kind == "forecast"].set_index("date").value])
        nxt = year_forecast(full, year + 1, window, growth_pct, method=cur.attrs["method"])
        rows.append({level: ent, f"{year} YTD": cur[cur.kind == "actual"].value.sum(),
                     f"{year} forecast": cur.value.sum(), f"{year + 1} budget": nxt.value.sum(),
                     "method": cur.attrs["method"], "MAPE": cur.attrs["mape"]})
    return pd.DataFrame(rows)
