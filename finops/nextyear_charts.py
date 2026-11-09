"""Charts for the Next Year Forecast page (ported from cloud_forecast.py / AWS_FinOps_By_Account.py / forecast_detail.py)."""
from __future__ import annotations
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .nextyear import NextYear
from .ui import COLORS, money

INK, INK2, MUTED, AXIS, GRID = "#0f172a", "#334155", "#64748b", "#cbd5e1", "#e2e8f0"
MKT_COLOR = {"AWS": "#B45309", "GCP": "#7C3AED"}       # marketplace tints, distinct from the provider colors


def rgba(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def _layout(fig: go.Figure, height: int, legend: bool = True) -> go.Figure:
    fig.update_layout(template="plotly_white", height=height, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      showlegend=legend, legend=dict(orientation="h", y=-0.18, font=dict(size=11)),
                      yaxis=dict(tickformat="$.2~s", gridcolor=GRID))
    return fig


def _forecast_marker(fig: go.Figure, ny: NextYear, text: str = "Forecast →") -> None:
    fig.add_vline(x=ny.horizon[0] - pd.Timedelta(days=15), line=dict(color=AXIS, width=1))
    fig.add_annotation(x=ny.horizon[0], y=1, yref="paper", text=text, showarrow=False, xanchor="left", font=dict(color=MUTED, size=11))


def fy_bridge(ny: NextYear, budgets: dict) -> go.Figure:
    """FY forecast vs budget per cloud (Services) + Marketplace; base with low/high error bars."""
    m = ny.months
    fy = {p: {sc: ny.svc[p].loc[m, sc].sum() for sc in ("Low", "Base", "High")} for p in ("AWS", "GCP")}
    mk_total = ny.mkt.loc[m].sum().sum()
    cats = ["AWS Services", "GCP Services", "Marketplace"]
    base = [fy["AWS"]["Base"], fy["GCP"]["Base"], mk_total]
    lo = [fy["AWS"]["Low"], fy["GCP"]["Low"], mk_total]
    hi = [fy["AWS"]["High"], fy["GCP"]["High"], mk_total]
    fig = go.Figure()
    fig.add_bar(x=cats, y=base, name=f"FY{ny.fy} base forecast", marker=dict(color=[COLORS["AWS"], COLORS["GCP"], MKT_COLOR["GCP"]]),
                error_y=dict(type="data", symmetric=False, array=[h - b for h, b in zip(hi, base)],
                             arrayminus=[b - l for b, l in zip(base, lo)], color=INK2, thickness=1.5, width=8),
                text=[money(v) for v in base], textposition="inside", insidetextanchor="start", textfont=dict(color="#fff", size=12),
                hovertemplate="%{x}: %{y:$,.0f}<extra></extra>")
    b = [budgets["AWS"] * 12, budgets["GCP"] * 12]
    if all(b):
        fig.add_scatter(x=cats[:2], y=b, name="FY budget", mode="markers",
                        marker=dict(symbol="line-ew", size=70, line=dict(width=3, color=INK)), hovertemplate="Budget %{y:$,.0f}<extra></extra>")
    fig = _layout(fig, 340)
    fig.update_layout(hovermode="closest", title=dict(text="FY forecast vs budget (bars = Low–High)", font=dict(size=13)))
    return fig


def services_chart(ny: NextYear, p: str, monthly_budget: float) -> go.Figure:
    col, act, f = COLORS[p], ny.svc_actual[p], ny.svc[p]
    fig = go.Figure()
    fig.add_scatter(x=list(f.index) + list(f.index[::-1]), y=list(f["High"]) + list(f["Low"][::-1]), fill="toself",
                    fillcolor=rgba(col, 0.18), line=dict(width=0), name="Low–High range", hoverinfo="skip")
    fig.add_scatter(x=act.index, y=act, name="Actual", mode="lines+markers", line=dict(color=col, width=2), marker=dict(size=7),
                    hovertemplate="%{y:$,.0f}")
    fig.add_scatter(x=[act.index[-1]] + list(f.index), y=[act.iloc[-1]] + list(f["Base"]), name="Base forecast", mode="lines+markers",
                    line=dict(color=col, width=2, dash="dash"), marker=dict(size=5), hovertemplate="%{y:$,.0f}")
    if monthly_budget:
        fig.add_hline(y=monthly_budget, line=dict(color=INK, width=1.5),
                      annotation=dict(text=f"{p} budget {money(monthly_budget)}/mo", font=dict(color=INK, size=11)), annotation_position="top left")
    _forecast_marker(fig, ny)
    last = f.iloc[-1]
    fig.add_annotation(x=f.index[-1], y=last["Base"], text=money(last["Base"]), showarrow=False, xanchor="left", xshift=8, font=dict(color=INK, size=11))
    fig = _layout(fig, 360)
    fig.update_layout(margin=dict(r=70), title=dict(text=f"{p} Cloud Services", font=dict(size=13)))
    fig.update_xaxes(tickformat="%b %y", dtick="M6", tickangle=0)
    fig.update_yaxes(range=[0, max(f["High"].max(), monthly_budget, act.max()) * 1.08])
    return fig


def marketplace_chart(ny: NextYear) -> go.Figure:
    fig = go.Figure()
    det = ny.mkt_detail
    for p in ("GCP", "AWS"):
        col = MKT_COLOR[p]
        fig.add_bar(x=ny.mkt_actual.index, y=ny.mkt_actual[p], name=f"{p} Marketplace (billed)", marker=dict(color=col), hovertemplate="%{y:$,.0f}")
        names = [", ".join(_s(c) for (pp, c), v in det.loc[d].items() if pp == p and v > 1000) for d in ny.mkt.index] if len(det.columns) else [""] * len(ny.mkt)
        fig.add_bar(x=ny.mkt.index, y=ny.mkt[p], name=f"{p} Marketplace (scheduled)", customdata=names,
                    marker=dict(color=rgba(col, 0.45), line=dict(color=col, width=1)),
                    hovertemplate="%{y:$,.0f}<br>%{customdata}<extra></extra>")
    fig.update_layout(barmode="stack")
    _forecast_marker(fig, ny)
    return _layout(fig, 380)


def _s(c: str, n: int = 24) -> str:
    return c if len(c) <= n else c[: n - 1] + "…"


def combined_chart(ny: NextYear, budgets: dict) -> go.Figure:
    fig = go.Figure()
    series = [("GCP Cloud Services", COLORS["GCP"], ny.svc_actual["GCP"], ny.svc["GCP"]["Base"]),
              ("AWS Cloud Services", COLORS["AWS"], ny.svc_actual["AWS"], ny.svc["AWS"]["Base"]),
              ("GCP Marketplace", MKT_COLOR["GCP"], ny.mkt_actual["GCP"], ny.mkt["GCP"]),
              ("AWS Marketplace", MKT_COLOR["AWS"], ny.mkt_actual["AWS"], ny.mkt["AWS"])]
    for name, col, a, f in series:
        fig.add_bar(x=list(a.index) + list(f.index), y=list(a) + list(f), name=name,
                    marker=dict(color=[col] * len(a) + [rgba(col, 0.5)] * len(f), line=dict(width=0)), hovertemplate="%{y:$,.0f}")
    fig.update_layout(barmode="stack")
    tb = budgets["AWS"] + budgets["GCP"]
    if tb:
        fig.add_hline(y=tb, line=dict(color=INK, width=1.5),
                      annotation=dict(text=f"Services budget (AWS+GCP) {money(tb)}/mo", font=dict(color=INK, size=11)), annotation_position="top left")
    _forecast_marker(fig, ny, "Forecast (base) →")
    return _layout(fig, 420)


def accounts_share(t: pd.DataFrame, ny: NextYear, p: str, label_len: int = 34) -> go.Figure:
    col = f"FY{ny.fy} forecast"
    t = t.iloc[::-1]
    fig = go.Figure(go.Bar(y=[_s(a, label_len) for a in t.index], x=t[col], orientation="h", marker=dict(color=COLORS[p]),
                           text=[money(v) for v in t[col]], textposition="outside", textfont=dict(color=INK2, size=11),
                           hovertemplate="%{y}: %{x:$,.0f}<extra></extra>"))
    fig = _layout(fig, 28 * len(t) + 70, legend=False)
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(tickformat="$.2~s", showgrid=True, gridcolor=GRID, range=[0, t[col].max() * 1.22])
    fig.update_yaxes(showgrid=False, tickfont=dict(color=INK2))
    return fig


def small_multiples(ny: NextYear, p: str, cols: int = 3) -> go.Figure:
    lines = ny.lines[p]
    color = COLORS[p]
    rows = int(np.ceil(len(lines) / cols))
    fig = make_subplots(rows=rows, cols=cols, subplot_titles=[f"{_s(lf.name, 28)} · {lf.model}" for lf in lines],
                        shared_xaxes=True, vertical_spacing=0.06 if rows > 4 else 0.1, horizontal_spacing=0.06)
    for i, lf in enumerate(lines):
        r, c = i // cols + 1, i % cols + 1
        fig.add_scatter(x=list(lf.forecast.index) + list(lf.forecast.index[::-1]), y=list(lf.hi) + list(lf.lo[::-1]), fill="toself",
                        fillcolor=rgba(color, 0.18), line=dict(width=0), hoverinfo="skip", showlegend=False, row=r, col=c)
        fig.add_scatter(x=lf.actual.index, y=lf.actual, mode="lines+markers", line=dict(color=color, width=2), marker=dict(size=5),
                        showlegend=False, hovertemplate="%{y:$,.0f}<extra>actual</extra>", row=r, col=c)
        fig.add_scatter(x=[lf.actual.index[-1]] + list(lf.forecast.index), y=[lf.actual.iloc[-1]] + list(lf.forecast), mode="lines",
                        line=dict(color=color, width=2, dash="dash"), showlegend=False, hovertemplate="%{y:$,.0f}<extra>forecast</extra>", row=r, col=c)
    fig = _layout(fig, 210 * rows + 40, legend=False)
    fig.update_layout(hovermode="closest", margin=dict(l=8, r=8, t=30, b=8))
    fig.update_annotations(font=dict(size=11, color=INK2))
    fig.update_xaxes(tickformat="%b %y", nticks=4)
    fig.update_yaxes(rangemode="tozero", nticks=4)
    return fig


def reconciliation(ny: NextYear, budgets: dict) -> go.Figure:
    m = ny.months
    cats = ["AWS Services", "GCP Services"]
    td = [ny.svc[p].loc[m, "Base"].sum() for p in ("AWS", "GCP")]
    bu = [ny.tables[p][f"FY{ny.fy} forecast"].sum() for p in ("AWS", "GCP")]
    fig = go.Figure()
    fig.add_bar(x=cats, y=td, name="Top-down (provider total, damped trend)", marker=dict(color="#475569"),
                text=[money(v) for v in td], textposition="outside", textfont=dict(color=INK2))
    fig.add_bar(x=cats, y=bu, name="Bottom-up (sum of per-account / per-project models)", marker=dict(color="#0F9D8A"),
                text=[money(v) for v in bu], textposition="outside", textfont=dict(color=INK2))
    bud = [budgets["AWS"] * 12, budgets["GCP"] * 12]
    for i, b in enumerate(bud):
        if b:
            fig.add_shape(type="line", xref="x", yref="y", x0=i - 0.45, x1=i + 0.45, y0=b, y1=b, line=dict(color=INK, width=2))
            fig.add_annotation(x=i + 0.45, y=b, text=f"Budget {money(b)}", showarrow=False, xanchor="left", xshift=4, font=dict(color=INK, size=11))
    fig.update_layout(barmode="group")
    fig = _layout(fig, 330)
    fig.update_layout(hovermode="closest")
    fig.update_yaxes(range=[0, max(td + bu + bud) * 1.2])
    return fig
