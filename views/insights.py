import pandas as pd
import streamlit as st
from finops.insights import (anomalies, new_spend, ownership_gap, spikes, structure_flags, sustained_growth,
                             top_movers)
from finops.ui import current_view, get_data, money, sidebar_filters

st.title("Insights & Savings")
df = sidebar_filters(get_data())
if df.empty:
    st.info("No data yet — use Data Health → Refresh.")
    st.stop()

st.caption("Rule-based and explainable. **Spend at stake** is the amount in question, not a promised saving — each "
           "item needs an owner's review.")
if current_view() != "Cloud Services (budget basis)":
    st.warning(f"These checks are designed for Cloud Services; you're viewing **{current_view()}**. "
               "For Marketplace use the Marketplace page.")

months = sorted(df.date.unique())
month = pd.Timestamp(st.sidebar.selectbox("Month", months[::-1], format_func=lambda d: pd.Timestamp(d).strftime("%b %Y")))
min_amt = st.sidebar.number_input("Materiality ($)", 500, 100000, 5000, 500,
                                  help="Ignore items smaller than this monthly amount")
level_name = st.sidebar.radio("Look at", ["Account / Project", "Service"])
level = "account" if level_name.startswith("Account") else "service"
st.sidebar.caption("History is ~14 months, so trailing windows are 3 months.")

flags = structure_flags(df, month)
sp = spikes(df, level, month, min_amt)
nw = new_spend(df, level, month, min_amt)
gr = sustained_growth(df, level, month, min_amt * 0.6)
og = ownership_gap(df, month)

k = st.columns(5)
k[0].metric("Structure flags", len(flags), f"{(flags.severity == 'High').sum()} high" if len(flags) else None, delta_color="off")
k[1].metric("Spikes", len(sp), money(sp.increase.sum()) + " above normal" if len(sp) else None, delta_color="off")
k[2].metric("New spend", len(nw), money(nw.this_month.sum()) if len(nw) else None, delta_color="off")
k[3].metric("Creeping growth", len(gr), money(gr.annualized.sum()) + "/yr" if len(gr) else None, delta_color="off")
k[4].metric("Unowned accounts", len(og), money(og.last_12mo.sum()) + " (12 mo)" if len(og) else None, delta_color="off")

tabs = st.tabs(["Overview", "Spikes & new spend", "Creeping growth", "Ownership", "Movers", "Anomalous months"])


def show(t: pd.DataFrame, money_cols=(), pct_cols=()):
    if t.empty:
        st.success("Nothing found at this threshold.")
        return
    st.dataframe(t.assign(**{c: t[c].map(money) for c in money_cols},
                          **{c: t[c].map(lambda v: "–" if pd.isna(v) else f"{v:+.0%}") for c in pct_cols}),
                 hide_index=True, width="stretch")


with tabs[0]:
    st.subheader(f"Cost-structure checks — {month:%b %Y}")
    if flags.empty:
        st.success("No structural flags.")
    for r in flags.itertuples():
        box = {"High": st.error, "Medium": st.warning}.get(r.severity, st.info)
        box(f"**{r.finding}** · {money(r.spend_at_stake)} at stake\n\n{r.evidence}")
with tabs[1]:
    st.subheader(f"Spikes vs trailing 3-month average ({level_name.lower()})")
    show(sp, ["trailing_3mo_avg", "this_month", "increase"], ["increase_pct"])
    st.subheader("New spend (nothing in the prior 3 months)")
    show(nw, ["prior_3mo_max", "this_month"])
with tabs[2]:
    st.subheader(f"Last 3 months vs the 3 before ({level_name.lower()})")
    show(gr, ["prior_3mo_avg", "last_3mo_avg", "increase_per_month", "annualized"], ["increase_pct"])
with tabs[3]:
    st.subheader("Accounts / projects with no owner")
    st.caption("Fix by filling `owner` in data/owners_aws.csv or data/owners_gcp.csv, then Reload data.")
    show(og, ["this_month", "last_12mo"])
with tabs[4]:
    for lv in ("service", "account"):
        st.subheader(f"Top movers by {lv} — {month:%b %Y} vs prior month")
        t = top_movers(df, lv, month)
        st.dataframe(t.assign(previous=t.previous.map(money), current=t.current.map(money), change=t.change.map(money),
                              change_pct=t.change_pct.map(lambda v: f"{v:+.0%}" if pd.notna(v) else "new")),
                     hide_index=True, width="stretch")
with tabs[5]:
    st.caption("Whole-provider months deviating > 2σ from the trailing 6-month mean (needs ~7+ months).")
    for p, g in df.groupby("provider"):
        a = anomalies(g.groupby("date")["cost_net"].sum())
        st.markdown(f"**{p}**")
        if len(a):
            st.dataframe(a.assign(cost=a.cost.map(money), trailing_mean=a.trailing_mean.map(money), z=a.z.round(1)),
                         hide_index=True, width="stretch")
        else:
            st.write("None")
