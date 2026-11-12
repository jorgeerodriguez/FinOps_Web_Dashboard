"""Shared provider page: Total / by account|project / by service, with drill-down, forecast and budget."""
from __future__ import annotations
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from .data import monthly
from .forecast import entity_table, forecast_two_years
from . import dims_ui
from .ui import COLORS, TEMPLATE, annual_budget, cfg, current_view, get_data, money, pct, show

ENTITY = {"AWS": "Account", "GCP": "Project"}


@st.cache_data(show_spinner="Forecasting each entity…")
def _entity_table(provider: str, level: str, year: int, window: int, growth: float) -> pd.DataFrame:
    return entity_table(_frame(provider, level), level, year, window, growth)


def _frame(provider: str, level: str) -> pd.DataFrame:
    return get_data().query("provider == @provider")


def fc_chart(f: pd.DataFrame, title: str, color: str) -> go.Figure:
    a, p = f[f.kind == "actual"], f[f.kind == "forecast"]
    fig = go.Figure()
    fig.add_bar(x=a.date, y=a.value, name="Actual", marker_color="#475569")
    fig.add_bar(x=p.date, y=p.value, name="Forecast", marker_color=color,
                error_y=dict(type="data", symmetric=False, array=p.high - p.value, arrayminus=p.value - p.low))
    fig.update_layout(template=TEMPLATE, title=title, yaxis_title="Net cost ($)", legend_orientation="h")
    return fig


def _report_button(provider: str) -> None:
    from .dims_report import build_report, save_report
    st.divider()
    st.markdown("**HTML report — Projects & Environments**")
    st.caption("Self-contained, print-ready (light theme). Covers AWS + GCP together, then each cloud: project codes, environments, projects on both clouds and Finance readiness. "
               "Always on the Cloud Services budget basis, whatever the sidebar shows.")
    if st.button("Build projects & environments report", key=f"{provider}_dims_report"):
        with st.spinner("Building report…"):
            html = build_report()
            path = save_report(html)
        st.session_state["dims_report"] = (html, path.name, str(path))
    if "dims_report" in st.session_state:
        html, name, path = st.session_state["dims_report"]
        st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key=f"{provider}_dims_dl")
        st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")


def render(provider: str) -> None:
    st.title(provider)
    st.caption(f"Spend category: **{current_view()}**")
    color = COLORS[provider]
    ent_label = ENTITY[provider]
    f = cfg()["forecast"]

    extra = {"Project code": "project_code", "Environment": "environment", ("Cost center" if provider == "GCP" else "Service name"):
             ("cost_center" if provider == "GCP" else "service_name")}
    level_name = st.sidebar.radio("View by", ["Total", ent_label, "Service", "Owner", "Family"] + list(extra))
    level = {"Total": None, ent_label: "account", "Service": "service", "Owner": "owner", "Family": "family", **extra}[level_name]
    window = st.sidebar.slider("YoY window (months)", 3, 12, f["yoy_window_months"])
    growth = st.sidebar.slider("Next-year adjustment (%)", -30.0, 30.0, float(f["default_growth_pct"]), 0.5)
    topn = st.sidebar.slider("Top N in charts", 5, 25, 12)

    df = _frame(provider, level or "service")
    if df.empty:
        st.warning("No data yet — open **Data Health** and click Refresh.")
        return
    last = df.date.max()

    # --- drill-down selection -------------------------------------------------------------
    sel = []
    if level:
        order = df[df.date > last - pd.DateOffset(months=12)].groupby(level)["cost_net"].sum().sort_values(ascending=False)
        sel = st.multiselect(f"Filter to {level_name.lower()}(s) — empty = all", list(order.index),
                             format_func=lambda x: f"{x}  ({money(order[x])} last 12 mo)")
        if sel:
            df = df[df[level].isin(sel)]
    scope = ", ".join(sel) if sel else f"All {provider}"

    # --- KPIs -----------------------------------------------------------------------------
    s = df.groupby("date")["cost_net"].sum()
    yr = last.year
    cur_m, prev_m = s.get(last, 0.0), s.get(last - pd.DateOffset(months=1), 0.0)
    ytd = s[s.index.year == yr].sum()
    _ly = s[(s.index.year == yr - 1) & (s.index.month <= last.month)]
    ytd_ly = _ly.sum() if len(_ly) == last.month else 0   # only if prior YTD is complete
    cur_f, nxt_f = forecast_two_years(df.assign(provider=provider), yr, window, growth)
    budget = annual_budget(yr, [provider]) if not sel else 0
    st.caption(f"Scope: **{scope}** · billed through {last:%B %Y}")
    k = st.columns(5)
    k[0].metric(f"{last:%b %Y}", money(cur_m), pct(cur_m / prev_m - 1) if prev_m else None, help="vs previous month")
    k[1].metric(f"YTD {yr}", money(ytd), pct(ytd / ytd_ly - 1) if ytd_ly else None, help="vs same period last year")
    k[2].metric(f"{yr} forecast", money(cur_f.value.sum()), help=f"Range {money(cur_f.low.sum())} – {money(cur_f.high.sum())}")
    k[3].metric(f"{yr + 1} baseline", money(nxt_f.value.sum()), pct(nxt_f.value.sum() / cur_f.value.sum() - 1) if cur_f.value.sum() else None)
    k[4].metric(f"{yr} vs budget", money(cur_f.value.sum() - budget) if budget else "n/a",
                pct(cur_f.value.sum() / budget - 1) if budget else None, delta_color="inverse",
                help=f"Annual budget {money(budget)}" if budget else "Budget applies to the full provider (no entity filter)")

    # --- trend ----------------------------------------------------------------------------
    t = df[df.date >= last - pd.DateOffset(months=23)]
    by = level or "service"
    level_for_top = by
    if True:
        top = t.groupby(by)["cost_net"].sum().nlargest(topn).index
        t = t.assign(**{by: t[by].where(t[by].isin(top), "Other")})
    show(px.bar(monthly(t, [by]), x="date", y="cost_net", color=by, template=TEMPLATE,
                           title=f"Monthly cost — {scope}" + (f" by {level_name.lower()}" if level else " by service"),
                           labels={"cost_net": "Net cost ($)", "date": ""}), width="stretch")

    # --- projects & environments ----------------------------------------------------------
    dims_ui.projects_section(df, provider, report_button=lambda: _report_button(provider))

    # --- forecast -------------------------------------------------------------------------
    st.subheader("Forecast")
    st.caption(f"Method: {cur_f.attrs['method']} · backtest error ≈ {cur_f.attrs['mape']:.0%} "
               "(bars = low/high band)" if pd.notna(cur_f.attrs["mape"]) else f"Method: {cur_f.attrs['method']}")
    a, b = st.columns(2)
    show(fc_chart(cur_f, f"{yr}: actual + forecast", color), container=a, width="stretch")
    show(fc_chart(nxt_f, f"{yr + 1}: budget projection ({growth:+.1f}% adj.)", color), container=b, width="stretch")

    # --- billing detail -------------------------------------------------------------------
    st.subheader("Billing detail")
    dim = level or "service"
    d = df[df.date.dt.year.isin([yr, yr - 1])]
    pv = d.pivot_table(index=dim, columns="date", values="cost_net", aggfunc="sum", fill_value=0.0)
    pv.columns = [c.strftime("%Y-%m") for c in pv.columns]
    pv.insert(0, f"Total {yr}", pv[[c for c in pv.columns if c.startswith(str(yr))]].sum(axis=1))
    pv = pv.sort_values(f"Total {yr}", ascending=False)
    st.dataframe(pv.round(0).rename_axis(columns=None), width="stretch")
    st.download_button("Download CSV", pv.round(2).to_csv().encode(), f"{provider}_{dim}_detail.csv", "text/csv")

    if level and not sel:
        st.subheader(f"Forecast & next-year budget by {level_name.lower()}")
        et = _entity_table(provider, level, yr, window, growth)
        if len(et):
            et = et.assign(**{c: et[c].map(money) for c in et.columns if c.startswith("20")},
                           MAPE=et["MAPE"].map(lambda v: "–" if pd.isna(v) else f"{v:.0%}"))
            st.dataframe(et, hide_index=True, width="stretch")
            st.caption("Entities are forecast individually (top 40 by last-12-month spend); they will not sum exactly to the provider total.")
