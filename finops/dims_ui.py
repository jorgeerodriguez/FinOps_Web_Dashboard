"""Streamlit blocks for project codes / environments / cost centers (logic and charts are in dims.py)."""
from __future__ import annotations
import pandas as pd
import streamlit as st

from . import dims as D
from .owners import MISSING
from .ui import money, show


def _coverage_strip(df: pd.DataFrame, cols: list[str]) -> None:
    bits = []
    for c in cols:
        cv = D.coverage(df, c)
        bits.append(f"**{D.LABEL[c]}:** {cv['share']:.0%} of spend" + (f" ({money(cv['missing'])} without)" if cv["missing"] >= 1 else ""))
    st.caption(" · ".join(bits) + " — fill the gaps in **Data Health → Owners**.")


def _table_block(df: pd.DataFrame, col: str, key: str, shared: list[str], n_chart: int = 12) -> None:
    t = D.by_dim(df, col, shared)
    st.dataframe(D.display(t, col), hide_index=True, width="stretch")
    st.download_button("Download CSV", t.drop(columns=[c for c in ("shared",) if c in t]).round(2).to_csv().encode(), f"{key}_{col}.csv", "text/csv", key=f"dl_{key}_{col}")
    if "shared" in t and t["shared"].any():
        st.caption("★ = shared / enterprise bucket (set in `config.toml` → `[dimensions] shared_codes`). Shown here, left out of the charts unless you untick the box.")


def projects_section(df: pd.DataFrame, provider: str, report_button=None) -> None:
    """Provider page: project codes, environments, the treemap and cost centers / service names for the rows in `df`."""
    st.subheader("Projects & environments")
    if df.empty:
        return
    shared = D.shared_codes()
    third = "cost_center" if provider == "GCP" else "service_name"
    _coverage_strip(df, ["project_code", "environment"] + (["cost_center"] if provider == "GCP" else []))
    k = st.columns(5)
    for col, it in zip(k, D.kpi_items(df, shared)):
        col.metric(it["label"], it["value"], it["delta"] or None, delta_color="off", help=it["help"])
    for line in D.narrative(df, shared):
        st.markdown(f"- {line}")

    t_code, t_env, t_tree, t_third = st.tabs(["Project codes", "Environments", "Treemap", "Cost centers" if provider == "GCP" else "Service names"])
    leave = st.session_state.get(f"{provider}_noshared", True)
    view = df[~D.is_shared(df["project_code"], shared)] if (leave and shared) else df
    with t_code:
        st.checkbox("Leave shared / enterprise buckets out of the charts", value=True, key=f"{provider}_noshared",
                    help="A support contract or commitment fee can be half the bill and flattens every other project. Tables always include them.")
        topn = st.slider("Project codes in the chart", 5, 20, 10, key=f"{provider}_pc_n")
        a, b = st.columns([3, 2])
        show(D.fig_stack(view, "project_code", topn), container=a, width="stretch")
        show(D.fig_change(D.by_dim(view, "project_code", shared), "project_code", 10), container=b, width="stretch")
        _table_block(df, "project_code", provider, shared)
    with t_env:
        a, b = st.columns(2)
        show(D.fig_env(df), container=a, width="stretch")
        show(D.fig_nonprod_share(df), container=b, width="stretch")
        e = D.env_by_code(df)
        st.markdown("**By project code** — where non-prod is large or growing")
        st.dataframe(D.env_display(e.head(25)), hide_index=True, width="stretch")
        grow = e[(e["non-prod"] >= 10_000) & (e["nonprod_6m_change"].fillna(0) >= 0.25)].sort_values("non-prod", ascending=False)
        if len(grow):
            st.warning("Non-prod is **growing** (last 6 months vs the 6 before, 25%+) for: " + ", ".join(f"{i} ({money(r['non-prod'])}, {D.chg(r['nonprod_6m_change'])})" for i, r in grow.head(6).iterrows())
                       + ". Worth asking the owner whether it should be switched off outside working hours or shrunk.")
        unspec = float(df[df["environment"] == "unspecified"]["cost_net"].sum())
        if unspec > 0:
            st.caption(f"Environment **unspecified** rows ({money(unspec)} over all months shown) are not counted in the non-prod share. Fix them in Data Health → Owners (choose *Environment unspecified*).")
    with t_tree:
        st.caption("Size = latest 12 months. Click a tile to zoom in; the strip at the top goes back up.")
        show(D.fig_treemap(df, shared, leave_out_shared=leave and bool(shared)), width="stretch")
    with t_third:
        if (view[third] != MISSING[third]).any():
            a, b = st.columns([3, 2])
            show(D.fig_stack(view, third, 10), container=a, width="stretch")
            show(D.fig_change(D.by_dim(view, third), third, 10), container=b, width="stretch")
            _table_block(df, third, provider, shared)
        else:
            st.info(f"No {D.LABEL[third].lower()} filled in for {provider} yet. Add it in **Data Health → Owners**.")
    if report_button:
        report_button()


def executive_section(df: pd.DataFrame, other_df: pd.DataFrame | None = None) -> None:
    """Executive Summary: project codes across AWS + GCP together, projects on both clouds, environment mix, Finance readiness."""
    st.subheader("Spend by project — AWS and GCP together")
    if df.empty:
        return
    shared = D.shared_codes()
    for line in D.narrative(df, shared)[:5]:
        st.markdown(f"- {line}")
    leave = st.checkbox("Leave shared / enterprise buckets out of the charts", value=bool(shared), key="exec_noshared", disabled=not shared)
    view = df[~D.is_shared(df["project_code"], shared)] if leave else df
    a, b = st.columns([3, 2])
    show(D.fig_stack(view, "project_code", 10, title="Monthly cost by project code (AWS + GCP)"), container=a, width="stretch")
    show(D.fig_change(D.by_dim(view, "project_code", shared), "project_code", 10), container=b, width="stretch")
    left, right = st.columns([3, 2])
    cc = D.cross_cloud(df)
    with left:
        if len(cc):
            st.markdown("**Projects that run on both clouds**")
            st.dataframe(D.cross_display(cc), hide_index=True, width="stretch")
        else:
            st.caption("No project code bills on both clouds above $5K a year each.")
    with right:
        show(D.fig_env(df, 12), width="stretch")
    st.markdown("**Finance readiness** — how much spend carries the codes Finance needs")
    st.dataframe(D.readiness_display(df), hide_index=True, width="stretch")
