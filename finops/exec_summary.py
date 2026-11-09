"""Executive Summary: numbers, figures and tables with NO Streamlit in them.
Used by views/executive.py (the page) and finops/exec_report.py (the HTML report), so they can never disagree."""
from __future__ import annotations
from dataclasses import dataclass

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .data import monthly
from .forecast import forecast_two_years
from .ui import COLORS, TEMPLATE, annual_budget, cfg, money, pct, ytd_budget


@dataclass
class Kpis:
    df: pd.DataFrame
    last: pd.Timestamp
    yr: int
    cur: float
    prev: float
    ly: float
    ytd: float
    ytd_ly: float
    s: pd.Series
    m: pd.DataFrame
    fc: pd.DataFrame
    fy: float
    budget: float
    ytd_b: float


def compute(df: pd.DataFrame) -> Kpis:
    last = df["date"].max()
    yr = last.year
    tot = lambda mask: df.loc[mask, "cost_net"].sum()
    _ly = df[(df["date"].dt.year == yr - 1) & (df["date"] <= last - pd.DateOffset(years=1))]
    provs = list(df["provider"].unique())
    fc = forecast_two_years(df, yr, cfg()["forecast"]["yoy_window_months"])[0]
    return Kpis(df=df, last=last, yr=yr, cur=tot(df["date"] == last), prev=tot(df["date"] == last - pd.DateOffset(months=1)),
                ly=tot(df["date"] == last - pd.DateOffset(years=1)), ytd=tot((df["date"].dt.year == yr) & (df["date"] <= last)),
                ytd_ly=_ly["cost_net"].sum() if _ly["date"].nunique() == last.month else 0,      # only if prior YTD is complete
                s=df.groupby("date")["cost_net"].sum(), m=monthly(df, ["provider"]), fc=fc, fy=fc["value"].sum(),
                budget=annual_budget(yr, provs), ytd_b=ytd_budget(yr, last.month, provs))


def kpi_items(k: Kpis) -> list[dict]:
    """label / value / delta / help / inverse (inverse = a rise is bad)."""
    return [
        dict(label=f"{k.last:%b %Y}", value=money(k.cur), delta=pct(k.cur / k.prev - 1 if k.prev else None), help="vs previous month"),
        dict(label="YoY (same month)", value=pct(k.cur / k.ly - 1 if k.ly else None), delta="", help=""),
        dict(label=f"YTD {k.yr}", value=money(k.ytd), delta=pct(k.ytd / k.ytd_ly - 1 if k.ytd_ly else None), help="vs same period last year"),
        dict(label=f"{k.yr} full-year forecast", value=money(k.fy), delta="", help=""),
        dict(label="Forecast vs budget", value=money(k.fy - k.budget) if k.budget else "n/a",
             delta=pct(k.fy / k.budget - 1) if k.budget else "", inverse=True,
             help=f"Annual budget {money(k.budget)}. YTD actual vs YTD budget: {money(k.ytd - k.ytd_b)}"),
    ]


def fig_provider_monthly(k: Kpis) -> go.Figure:
    return px.bar(k.m[k.m["date"] >= k.last - pd.DateOffset(months=23)], x="date", y="cost_net", color="provider",
                  color_discrete_map=COLORS, template=TEMPLATE, labels={"cost_net": "Net cost ($)", "date": ""})


def fig_vs_budget(k: Kpis) -> tuple[go.Figure, str]:
    f = k.fc.copy()
    f["m"] = f["date"].dt.strftime("%b")
    a_, p_ = f[f.kind == "actual"], f[f.kind == "forecast"]
    ly_s = k.s.reindex(f["date"] - pd.DateOffset(years=1))
    ly_s.index = f["m"]
    fig = go.Figure()
    fig.add_bar(x=a_["m"], y=a_["value"], name=f"{k.yr} actual", marker_color="#334155")
    fig.add_bar(x=p_["m"], y=p_["value"], name=f"{k.yr} forecast", marker_color="#93c5fd",
                error_y=dict(type="data", symmetric=False, array=p_["high"] - p_["value"], arrayminus=p_["value"] - p_["low"]))
    have = ly_s.dropna()
    if len(have):
        fig.add_scatter(x=have.index, y=have.values, mode="markers", name=f"{k.yr - 1} same month",
                        marker=dict(symbol="diamond", size=11, color="#f59e0b", line=dict(width=1, color="white")))
    if k.budget:
        fig.add_hline(y=k.budget / 12, line_dash="dash", line_color="#dc2626",
                      annotation_text=f"Budget {money(k.budget / 12)}/mo", annotation_position="top left")
    fig.update_layout(template=TEMPLATE, yaxis_title="Net cost ($)", legend_orientation="h", legend_y=-0.2,
                      xaxis=dict(categoryorder="array", categoryarray=list(f["m"])))
    cap = (f"{k.yr - 1} markers appear only for months we hold data for ({', '.join(have.index) if len(have) else 'none'}). "
           "Lighter bars = forecast (bars show low/high band).")
    return fig, cap


def fig_cumulative(k: Kpis) -> tuple[go.Figure, tuple[str, str] | None, str]:
    f2 = k.fc.copy()
    f2["m"] = f2["date"].dt.strftime("%b")
    f2["cum"], f2["cum_lo"], f2["cum_hi"] = f2["value"].cumsum(), f2["low"].cumsum(), f2["high"].cumsum()
    act = f2[f2.kind == "actual"]
    frc = pd.concat([act.tail(1), f2[f2.kind == "forecast"]])
    fig = go.Figure()
    if len(frc) > 1:
        fig.add_scatter(x=frc["m"], y=frc["cum_hi"], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip")
        fig.add_scatter(x=frc["m"], y=frc["cum_lo"], mode="lines", line=dict(width=0), fill="tonexty",
                        fillcolor="rgba(147,197,253,0.35)", name="Forecast range", hoverinfo="skip")
        fig.add_scatter(x=frc["m"], y=frc["cum"], mode="lines+markers", name="Forecast", line=dict(color="#2563EB", dash="dot"))
    fig.add_scatter(x=act["m"], y=act["cum"], mode="lines+markers", name="Actual (cumulative)", line=dict(color="#334155", width=3))
    if k.budget:
        fig.add_scatter(x=f2["m"], y=[k.budget / 12 * (i + 1) for i in range(12)], mode="lines", name="Budget (cumulative)",
                        line=dict(color="#dc2626", dash="dash"))
        fig.add_hline(y=k.budget, line_color="#dc2626", line_width=1, opacity=0.4,
                      annotation_text=f"Annual budget {money(k.budget)}", annotation_position="top left")
    fig.update_layout(template=TEMPLATE, yaxis_title="Cumulative net cost ($)", legend_orientation="h", legend_y=-0.15,
                      xaxis=dict(categoryorder="array", categoryarray=list(f2["m"])), margin=dict(t=30))
    verdict = None
    if k.budget:
        over, gap = f2[f2["cum"] >= k.budget], k.fy - k.budget
        verdict = (("error", f"At this pace the annual budget ({money(k.budget)}) is used up in **{over.iloc[0]['m']} {k.yr}**, "
                             f"finishing the year {money(gap)} ({gap / k.budget:+.1%}) over.") if len(over) else
                   ("success", f"At this pace the year finishes {money(-gap)} ({-gap / k.budget:.1%}) **under** the {money(k.budget)} annual budget."))
        cap = (f"YTD through {k.last:%b}: {money(k.ytd)} actual vs {money(k.ytd_b)} budgeted ({money(k.ytd - k.ytd_b)} "
               f"{'over' if k.ytd > k.ytd_b else 'under'}).")
    else:
        cap = "No budget line: budgets apply to Cloud Services only (choose that view in the sidebar)."
    return fig, verdict, cap


def separate_table(allc: pd.DataFrame, k: Kpis) -> pd.DataFrame:
    allc = allc[allc["provider"].isin(k.df["provider"].unique())]
    rows = []
    for cat in ("Marketplace", "Tax"):
        d = allc[allc["category"] == cat]
        rows.append({"Category": cat, f"{k.last:%b %Y}": money(d.loc[d.date == k.last, "cost_net"].sum()),
                     f"YTD {k.yr}": money(d.loc[d.date.dt.year == k.yr, "cost_net"].sum()),
                     f"{allc.date.min():%b %Y} – Dec {k.yr - 1} (partial)": money(d.loc[d.date.dt.year == k.yr - 1, "cost_net"].sum())})
    return pd.DataFrame(rows)


def top_services(k: Kpis, n: int = 10) -> pd.DataFrame:
    top = k.df[k.df["date"] == k.last].groupby(["provider", "service"], as_index=False)["cost_net"].sum().nlargest(n, "cost_net")
    return top.assign(cost_net=top["cost_net"].map(money))
