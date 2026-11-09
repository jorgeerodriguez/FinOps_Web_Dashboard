"""Charts for the Month-to-Date page (no Streamlit)."""
from __future__ import annotations
import pandas as pd
import plotly.graph_objects as go

from .asnow import AsNow, by_level
from .ui import COLORS, TEMPLATE, money


def budget_chart(a: AsNow) -> go.Figure:
    cats, rows = ["AWS Cloud Services", "GCP Cloud Services", "Total"], [a.prov["AWS"], a.prov["GCP"], a.total]
    fig = go.Figure()
    fig.add_bar(x=cats, y=[r["last"] for r in rows], name=f"{a.prev:%b} (full month)", marker_color="#94a3b8")
    fig.add_bar(x=cats, y=[r["mtd"] for r in rows], name=f"{a.month:%b} so far", marker_color="#334155",
                text=[money(r["mtd"]) for r in rows], textposition="inside", textfont=dict(color="#fff"))
    fig.add_bar(x=cats, y=[r["projected"] for r in rows], name=f"{a.month:%b} projected", marker_color=[COLORS["AWS"], COLORS["GCP"], "#2563EB"],
                error_y=dict(type="data", symmetric=False, array=[r["high"] - r["projected"] for r in rows],
                             arrayminus=[r["projected"] - r["low"] for r in rows], color="#475569", thickness=1.5, width=6),
                text=[money(r["projected"]) for r in rows], textposition="inside", insidetextanchor="start", textfont=dict(color="#fff"))
    if all(r["budget"] for r in rows):
        fig.add_scatter(x=cats, y=[r["budget"] for r in rows], mode="markers", name="Monthly budget",
                        marker=dict(symbol="line-ew", size=70, line=dict(width=3, color="#dc2626")), hovertemplate="Budget %{y:$,.0f}<extra></extra>")
    fig.update_layout(template=TEMPLATE, barmode="group", yaxis_title="Net cost ($)", legend_orientation="h", legend_y=-0.15,
                      title=dict(text=f"{a.month:%B %Y}: month to date and projected month-end vs budget", font=dict(size=14)))
    return fig


def movers_chart(a: AsNow, level: str, n: int = 8) -> go.Figure:
    g = by_level(a, level)
    g = g[g["last"] + g["projected"] > 500]
    top = pd.concat([g[g.change > 0].head(n), g[g.change < 0].head(n)]).sort_values("change")
    fig = go.Figure(go.Bar(y=[s[:34] for s in top[level]], x=top["change"], orientation="h",
                           marker_color=["#dc2626" if v > 0 else "#16a34a" for v in top["change"]],
                           text=[("+" if v > 0 else "−") + money(abs(v)) for v in top["change"]], textposition="outside", cliponaxis=False,
                           customdata=top[["mtd", "last", "projected"]], hovertemplate="%{y}<br>so far %{customdata[0]:$,.0f} · last month %{customdata[1]:$,.0f} · projected %{customdata[2]:$,.0f}<extra></extra>"))
    fig.update_layout(template=TEMPLATE, height=max(320, 30 * len(top) + 90), margin=dict(l=10, r=60, t=40, b=10), xaxis=dict(tickformat="$.2~s"),
                      title=dict(text=f"Projected {a.month:%b} vs {a.prev:%b}, by {'service' if level == 'service' else 'account / project'} (red = up, green = down)", font=dict(size=14)))
    return fig
