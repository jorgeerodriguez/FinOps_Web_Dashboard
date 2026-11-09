"""As-now / month-to-date view (no Streamlit). The in-progress month is stored with is_partial=True by the refresh.

Projecting a month-end from month-to-date is NOT just MTD / days x days, because many charges post as lumps
(Support, Savings Plans, annual fees). Per service line (provider, account, service) we therefore use:

  already billed   MTD >= 85% of last month's amount, early in the month (< 60% elapsed)  -> a lump: projected = MTD
  not posted yet   MTD == 0 but last month had it                                         -> assume last month's amount
  ahead of pace    run-rate would exceed 1.6x last month on a line >= $1K                -> capped at 1.5x last month (watch list)
  continuous       everything else                                                        -> MTD / elapsed fraction

The uncertainty band shrinks as the month progresses: +/- 30% x (share of the month still to come), at least 5%.
Marketplace = MTD + the contract calendar's scheduled-but-not-yet-billed charges. Tax = observed rate x (services + marketplace).
"""
from __future__ import annotations
import calendar
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import marketplace as mkt
from . import store
from .schema import CLOUD, MARKETPLACE, TAX
from .ui import annual_budget, force_view

PROV = ("AWS", "GCP")
LUMP_RATIO, LUMP_BEFORE = 0.85, 0.60
AHEAD_TRIGGER, AHEAD_CAP = 1.6, 1.5
CLOUD_VIEW = "Cloud Services (budget basis)"


@dataclass
class AsNow:
    month: pd.Timestamp
    prev: pd.Timestamp
    as_of: pd.Timestamp               # last complete day included
    n_days: int
    month_days: int
    frac: float
    refreshed: dict
    stale: bool
    lines: pd.DataFrame = None        # provider, account, service, mtd, last, projected, method
    prov: dict = field(default_factory=dict)
    total: dict = field(default_factory=dict)
    mkt_detail: pd.DataFrame = None
    confidence: str = ""


def freshness(month: pd.Timestamp) -> tuple[pd.Timestamp, dict, bool]:
    """Last COMPLETE day in the data = (oldest provider refresh date) - 1. stale if that refresh was in an earlier month."""
    log = store.read_log()
    ref = {}
    for p in PROV:
        r = log[log["provider"] == p]
        ref[p] = pd.Timestamp(r["when"].max()) if len(r) else None
    known = [v for v in ref.values() if v is not None]
    if not known:
        return month - pd.Timedelta(days=1), ref, True
    oldest = min(known)
    as_of = (oldest.normalize() - pd.Timedelta(days=1))
    stale = oldest.to_period("M") != month.to_period("M")
    return min(as_of, month + pd.offsets.MonthEnd(0)), ref, stale


def _project_lines(mtd: pd.Series, last: pd.Series, frac: float) -> pd.DataFrame:
    idx = mtd.index.union(last.index)
    df = pd.DataFrame({"mtd": mtd.reindex(idx).fillna(0.0), "last": last.reindex(idx).fillna(0.0)})
    lump = (df["last"] > 0) & (df["mtd"] >= LUMP_RATIO * df["last"]) & (frac < LUMP_BEFORE)
    notyet = (df["mtd"] <= 0) & (df["last"] > 0)
    run = df["mtd"] / max(frac, 1e-9)
    ahead = ~lump & ~notyet & (df["last"] >= 1000) & (run > AHEAD_TRIGGER * df["last"])
    df["method"] = np.select([lump, notyet, ahead], ["Already billed", "Not posted yet → last month", "Ahead of pace (capped)"], default="Run-rate")
    df["projected"] = np.select([lump, notyet, ahead], [df["mtd"], df["last"], np.minimum(run, AHEAD_CAP * df["last"])], default=run)
    return df


def compute(df_all: pd.DataFrame) -> AsNow | None:
    """df_all: store rows INCLUDING the in-progress month (get_data(include_partial=True, apply_view=False))."""
    part = df_all[df_all["is_partial"].astype(bool)]
    if part.empty:
        return None
    month = part["date"].max()
    prev = month - pd.DateOffset(months=1)
    as_of, ref, stale = freshness(month)
    month_days = calendar.monthrange(month.year, month.month)[1]
    n = int(np.clip((as_of - month).days + 1, 1, month_days))
    frac = n / month_days
    a = AsNow(month, prev, as_of, n, month_days, frac, ref, stale)
    a.confidence = "Low (first 10 days)" if n < 10 else "Medium" if frac < 0.6 else "High"

    key = ["provider", "account", "service"]
    cloud = df_all[df_all["category"] == CLOUD]
    mtd = cloud[cloud["date"] == month].groupby(key)["cost_net"].sum()
    last = cloud[(cloud["date"] == prev) & ~cloud["is_partial"].astype(bool)].groupby(key)["cost_net"].sum()
    lines = _project_lines(mtd, last, frac).reset_index()
    a.lines = lines

    # ---- marketplace: MTD + scheduled-but-not-yet-billed from the contract calendar
    billed = df_all[~df_all["is_partial"].astype(bool)]
    m_billed = billed[billed["category"] == MARKETPLACE]
    mk_mtd = df_all[(df_all["category"] == MARKETPLACE) & (df_all["date"] == month)]
    remaining = {p: 0.0 for p in PROV}
    detail = []
    if len(m_billed):
        ct, m, _ = mkt.build(m_billed, month)
        sched = m.loc[m.index == month].T.iloc[:, 0] if month in m.index else pd.Series(dtype=float)
        mk_mtd_c = mk_mtd.assign(contract=mk_mtd["service"].map(mkt.contract_key)).groupby(["provider", "contract"])["cost_net"].sum()
        for (p, c), v in sched.items():
            if v > 0:
                got = float(mk_mtd_c.get((p, c), 0.0))
                rem = max(v - got, 0.0)
                remaining[p] += rem
                detail.append({"provider": p, "contract": c, "vendor": mkt.vendor_label(c), "scheduled": v, "billed so far": got, "still expected": rem})
    a.mkt_detail = pd.DataFrame(detail)

    # ---- tax rate from the last 12 billed months
    b12 = billed[billed["date"] > prev - pd.DateOffset(months=12)]
    tax_rate = {}
    for p in PROV:
        t = b12[(b12.provider == p) & (b12.category == TAX)].cost_net.sum()
        base = b12[(b12.provider == p) & (b12.category.isin([CLOUD, MARKETPLACE]))].cost_net.sum()
        tax_rate[p] = t / base if base else 0.0

    band = max(0.05, 0.30 * (1 - frac))
    with force_view(CLOUD_VIEW):
        for p in PROV:
            ln = lines[lines.provider == p]
            proj = float(ln["projected"].sum())
            b = annual_budget(month.year, [p]) / 12
            mm = float(mk_mtd[mk_mtd.provider == p].cost_net.sum())
            mp = mm + remaining[p]
            tx_mtd = float(df_all[(df_all.provider == p) & (df_all.category == TAX) & (df_all.date == month)].cost_net.sum())
            a.prov[p] = dict(mtd=float(ln["mtd"].sum()), projected=proj, low=proj * (1 - band), high=proj * (1 + band), last=float(ln["last"].sum()),
                             budget=b, naive=float(ln["mtd"].sum() / frac), mkt_mtd=mm, mkt_projected=mp, mkt_remaining=remaining[p],
                             tax_mtd=tx_mtd, tax_projected=tax_rate[p] * (proj + mp))
    keys = ("mtd", "projected", "low", "high", "last", "budget", "naive", "mkt_mtd", "mkt_projected", "mkt_remaining", "tax_mtd", "tax_projected")
    a.total = {k: sum(a.prov[p][k] for p in PROV) for k in keys}
    a.total["band"] = band
    return a


def by_level(a: AsNow, level: str) -> pd.DataFrame:
    """Projected vs last month, aggregated to 'service' or 'account'."""
    g = a.lines.groupby(level)[["mtd", "last", "projected"]].sum()
    g["change"] = g["projected"] - g["last"]
    g["change_pct"] = g["change"] / g["last"].where(g["last"] > 100)
    return g.sort_values("change", key=lambda s: s.abs(), ascending=False).reset_index()


def lump_summary(a: AsNow) -> pd.DataFrame:
    """Lines treated as already billed / not posted yet (the ones a plain run-rate would get wrong)."""
    l = a.lines[a.lines.method != "Run-rate"].copy()
    l["effect_vs_naive"] = l["projected"] - l["mtd"] / a.frac
    return l.sort_values("effect_vs_naive", key=lambda s: s.abs(), ascending=False)
