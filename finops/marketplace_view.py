"""Marketplace calendar: numbers, figures, tables (no Streamlit). Shared by views/marketplace.py and the HTML report."""
from __future__ import annotations
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .marketplace import build, stale_charges
from .ui import TEMPLATE, heat_scale, money


def assemble(mkt: pd.DataFrame, group: str = "Vendor", topn: int = 30, hist_months: int | None = None) -> dict:
    """hist_months: how many billed months the calendar and cash view show (None = all we hold). Totals always use everything."""
    last0 = mkt.date.max()
    yr = last0.year
    ct, m, last = build(mkt, pd.Timestamp(yr + 1, 12, 1))
    lab = {(r.provider, r.contract): (r.vendor if group == "Vendor" else r.contract) for r in ct.itertuples()}
    long = (m.stack([0, 1]).rename("amount").reset_index().rename(columns={"level_1": "provider", "level_2": "contract"}))
    long["row"] = [lab[(p, c)] for p, c in zip(long.provider, long.contract)]
    long["kind"] = np.where(long.date <= last, "actual", "forecast")
    long = long[long.amount > 0]

    fy_cur = long[long.date.dt.year == yr].amount.sum()
    fy_nxt = long[long.date.dt.year == yr + 1].amount.sum()
    ytd = long[(long.date.dt.year == yr) & (long.kind == "actual")].amount.sum()
    next90 = long[(long.kind == "forecast") & (long.date <= last + pd.DateOffset(months=3))].amount.sum()
    ytd_ly = long[(long.date.dt.year == yr - 1) & (long.date.dt.month <= last.month) & (long.kind == "actual")].amount.sum()
    have_ly = m.index.min() <= pd.Timestamp(yr - 1, 1, 1) and ytd_ly > 0
    kpis = [
        dict(label=f"YTD {yr} billed", value=money(ytd), delta=f"{ytd / ytd_ly - 1:+.0%} vs YTD {yr - 1}" if have_ly else "", neutral=True),
        dict(label=f"{yr} full year (billed + scheduled)", value=money(fy_cur), delta=""),
        dict(label=f"{yr + 1} commitments", value=money(fy_nxt), delta=f"{fy_nxt / fy_cur - 1:+.0%} vs {yr}" if fy_cur else "", neutral=True),
        dict(label="Next 3 months scheduled", value=money(next90), delta=""),
        dict(label="Active contracts", value=str(int((ct.cadence != "Not billed in last 12 mo").sum())), delta=""),
    ]

    # ---- calendar heatmap (only the months asked for: the contracts' history is kept in the totals, not all drawn)
    shown = m.index if not hist_months else m.index[m.index >= last - pd.DateOffset(months=hist_months - 1)]
    long = long[long.date.isin(shown)] if hist_months else long
    rows = long.groupby("row").amount.sum().nlargest(topn).index
    cal = long[long.row.isin(rows)].pivot_table(index="row", columns="date", values="amount", aggfunc="sum").reindex(rows[::-1])
    x = [d.strftime("%b %y") for d in shown]
    cal = cal.reindex(columns=shown)
    is_act = np.array([d <= last for d in cal.columns])
    mask = np.tile(is_act, (len(cal), 1))
    z_act = cal.where(mask, np.nan).replace(0, np.nan)
    z_fc = cal.where(~mask, np.nan).replace(0, np.nan)
    fmt = lambda v: "" if pd.isna(v) else (f"{v:,.0f}" if v < 1000 else f"{v / 1e3:,.0f}K" if v < 1e6 else f"{v / 1e6:.2f}M")
    fig = go.Figure()
    fig.add_trace(go.Heatmap(z=z_act.values, x=x, y=list(cal.index), colorscale=heat_scale("blue"), showscale=False,
                             text=np.vectorize(fmt)(z_act.values), texttemplate="%{text}", hoverongaps=False,
                             hovertemplate="%{y}<br>%{x}: $%{z:,.0f} (billed)<extra></extra>", xgap=2, ygap=2))
    fig.add_trace(go.Heatmap(z=z_fc.values, x=x, y=list(cal.index), colorscale=heat_scale("purple"), showscale=False,
                             text=np.vectorize(fmt)(z_fc.values), texttemplate="%{text}", hoverongaps=False,
                             hovertemplate="%{y}<br>%{x}: $%{z:,.0f} (scheduled)<extra></extra>", xgap=2, ygap=2))
    boundary = int(is_act.sum()) - 0.5
    fig.add_vline(x=boundary, line_dash="dash", line_color="#64748b")
    fig.add_annotation(x=boundary + 0.1, y=-0.01, yref="paper", yanchor="top", text="forecast →", showarrow=False, xanchor="left", font=dict(color="#7c3aed"))
    fig.add_annotation(x=boundary - 0.1, y=-0.01, yref="paper", yanchor="top", text="← billed", showarrow=False, xanchor="right", font=dict(color="#2563eb"))
    fig.update_layout(template=TEMPLATE, height=max(380, 26 * len(cal) + 120), margin=dict(l=10, r=10, t=40, b=30),
                      xaxis=dict(side="top", type="category"), yaxis=dict(automargin=True))
    cal_caption = ("Blue = billed · purple = scheduled from the contract's cadence. Cells in $ (K = thousands, M = millions). "
                   f"Showing top {len(cal)} {group.lower()}s by total.")

    # ---- stacked monthly bars
    top = long.groupby("row").amount.sum().nlargest(8).index
    b = long.assign(row=long.row.where(long.row.isin(top), "Other")).groupby(["date", "row", "kind"], as_index=False).amount.sum()
    bars = px.bar(b, x="date", y="amount", color="row", pattern_shape="kind", template=TEMPLATE,
                  pattern_shape_map={"actual": "", "forecast": "/"}, labels={"amount": "$", "date": "", "row": group})
    for tr in bars.data:                       # one legend entry per vendor; hatched = scheduled
        if tr.name.endswith(", forecast"):
            tr.showlegend = False
        else:
            tr.name = tr.name.rsplit(", ", 1)[0]
    bars.update_layout(legend=dict(font=dict(size=10), title=None))

    # ---- tables
    quarter = (long[long.date.dt.year == yr + 1].assign(Q=lambda d: "Q" + d.date.dt.quarter.astype(str)).groupby("Q").amount.sum())
    t = ct.copy()
    full = (m.stack([0, 1]).rename("amount").reset_index().rename(columns={"level_1": "provider", "level_2": "contract"}))
    full = full[full.amount > 0]
    sel = lambda p, c, y: full[(full.provider == p) & (full.contract == c) & (full.date.dt.year == y)]
    t[f"{yr} (billed + sched.)"] = [sel(p, c, yr).amount.sum() for p, c in zip(t.provider, t.contract)]
    t[f"{yr + 1} forecast"] = [sel(p, c, yr + 1).amount.sum() for p, c in zip(t.provider, t.contract)]
    t[f"billing months {yr + 1}"] = [", ".join(sel(p, c, yr + 1).date.dt.strftime("%b")) or "—" for p, c in zip(t.provider, t.contract)]
    t = t.sort_values(f"{yr + 1} forecast", ascending=False)
    return dict(yr=yr, last=last, ct=ct, m=m, long=long, kpis=kpis, cal=fig, cal_caption=cal_caption, bars=bars,
                quarter=quarter, contracts=t, risk=stale_charges(mkt, ct), group=group)


def _yoy(cur, prev):
    if pd.isna(prev):
        return "–"
    if prev < 100:
        return "new" if cur >= 100 else "–"
    return f"{cur / prev - 1:+.0%}"


def contracts_display(t: pd.DataFrame, yr: int) -> pd.DataFrame:
    yoy = [_yoy(c, p) for c, p in zip(t.billed_12mo, t.billed_prev12)] if "billed_prev12" in t else ["–"] * len(t)
    return t.assign(**{"vs prior 12 mo": yoy}).assign(amount=t.amount.map(money), billed_12mo=t.billed_12mo.map(money),
                    next_charge=t.next_charge.dt.strftime("%b %Y").fillna("—"),
                    **{f"{yr} (billed + sched.)": t[f"{yr} (billed + sched.)"].map(money),
                       f"{yr + 1} forecast": t[f"{yr + 1} forecast"].map(money)}
                    ).rename(columns={"amount": "per charge", "billed_12mo": "billed last 12 mo"})


def risk_display(risk: pd.DataFrame) -> pd.DataFrame:
    return (risk.assign(amount=risk.amount.map(money), last_billed=risk.last_billed.dt.strftime("%b %Y"))
            .rename(columns={"provider": "Provider", "contract": "Contract", "vendor": "Vendor", "amount": "Billed", "last_billed": "Last billed"}))
