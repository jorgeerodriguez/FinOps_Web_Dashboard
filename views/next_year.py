import pandas as pd
import streamlit as st
from finops import editor_ui
from finops import history as hist
from finops import nextyear as ny_mod
from finops import nextyear_charts as ch
from finops import nextyear_summary as nys
from finops.nextyear_report import build_report, save_report
from finops.ui import bad_color, get_data, good_color, money, show

st.title("Next Year Forecast")


@st.cache_data(show_spinner="Building next-year forecast (model competition per account / project)… ~30 s the first time")
def _build(top_n: int, data_stamp: float, adjust: bool):
    return ny_mod.build(get_data(apply_view=False), top_n, adjust)


df_all = get_data(apply_view=False)
if df_all.empty:
    st.info("No data yet — use Data Health → Refresh.")
    st.stop()

# ----------------------------------------------------------------------------- sidebar
top_n = st.sidebar.slider("Accounts / projects modelled individually", 6, 20, 12)
scenario = st.sidebar.radio("Scenario for tables", ny_mod.SCEN, index=1)
adjust = st.sidebar.checkbox("Apply reviewed anomaly adjustments", value=True,
                             help="Billing gaps you confirmed (data/anomaly_rules.csv) are added back to the trend, and GCP's trend skips its launch "
                                  "ramp (config: forecast.trend_since). Untick to see the raw model.")
st.sidebar.markdown("**FY monthly budget (Cloud Services)**")
ny = _build(top_n, float(len(df_all)), adjust)
fy = ny.fy
dflt, assumed = nys.default_budgets(fy)
b_aws = st.sidebar.number_input("AWS $/month", 0, 5_000_000, dflt["AWS"], 10_000)
b_gcp = st.sidebar.number_input("GCP $/month", 0, 5_000_000, dflt["GCP"], 10_000)
if assumed:
    st.sidebar.caption(f"FY{fy} budget not set in config.toml — defaulting to FY{fy - 1} values (change above, session only).")
budgets = {"AWS": b_aws, "GCP": b_gcp}
sm = nys.summarize(ny, budgets)

st.caption(f"Built from the last 12 billed months ({ny.basis[0]:%b %Y} – {ny.last:%b %Y}); earlier months are shown in grey for context. Cloud Services only is held to the budget; "
           "Marketplace is planned separately from the renewal calendar. Method in the expander at the bottom.")

# ----------------------------------------------------------------------------- 1. leadership summary
st.subheader(f"FY{fy} at a glance")
for col, it in zip(st.columns(5), nys.kpi_items(ny, sm)):
    col.metric(it["label"], it["value"], it["delta"] or None,
               delta_color="inverse" if it.get("inverse") else "off", help=it["help"] or None)
for t in nys.bullets(ny, sm):
    st.markdown(f"- {t}")
if not adjust:
    st.warning("Anomaly adjustments are OFF — showing the raw model.")

# ----------------------------------------------------------------------------- 2. bridge + FY table
c1, c2 = st.columns([3, 2])
show(ch.fy_bridge(ny, budgets), container=c1, width="stretch")
with c2:
    st.markdown("**FY summary (Cloud Services)**")
    ft = nys.fy_summary_df(ny, sm)
    sty = ft.style.format(lambda v: "–" if pd.isna(v) else money(v)).map(
        lambda v: (f"color:{good_color()};font-weight:600" if v >= 0 else f"color:{bad_color()};font-weight:600") if pd.notna(v) else "", subset=["Var. (Base)"])
    st.dataframe(sty, width="stretch")
    st.caption("Variance = budget − forecast: green = under budget, red = over.")

# ----------------------------------------------------------------------------- 3. provider charts
st.subheader("Cloud Services — actual, base forecast and range")
a, b = st.columns(2)
show(ch.services_chart(ny, "AWS", budgets["AWS"]), container=a, width="stretch")
show(ch.services_chart(ny, "GCP", budgets["GCP"]), container=b, width="stretch")


# ----------------------------------------------------------------------------- 4. tables
def var_style(df: pd.DataFrame, var_rows=(), var_cols=()):
    def f(r):
        out = []
        for c in df.columns:
            v = r[c]
            isvar = (r.name in var_rows) or (c in var_cols)
            out.append((f"color:{good_color()};font-weight:600" if v >= 0 else f"color:{bad_color()};font-weight:600") if isvar and abs(v) >= 0.5 else "")
        return out
    return df.style.format(lambda v: money(v) if abs(v) >= 0.5 else "$0").apply(f, axis=1)


st.subheader("Budget tables")
t1, t2 = st.tabs(["By quarter — Services vs budget", "By month"])
with t1:
    qt = nys.quarterly_df(ny, budgets)
    st.dataframe(var_style(qt, var_rows=[r for r in qt.index if "Variance" in r]), width="stretch")
    st.caption("Variance = budget − forecast. Green = under budget, red = over.")
with t2:
    view = st.radio("View", ["Services", "Marketplace", "Combined"], horizontal=True)
    st.dataframe(var_style(nys.monthly_df(ny, budgets, view, scenario), var_cols=["Variance"]), width="stretch")
    st.caption(f"Scenario shown for Services: **{scenario}**.")

# ----------------------------------------------------------------------------- 5. Marketplace + combined
st.subheader("Marketplace — contract renewal calendar")
show(ch.marketplace_chart(ny), width="stretch")
cL, cR = st.columns([1, 2])
cL.dataframe(nys.marketplace_quarter_df(ny).style.format(lambda v: money(v)), width="stretch")
with cR:
    tc = nys.top_contracts_display(ny)
    if tc is not None:
        st.dataframe(tc, hide_index=True, width="stretch")
        st.caption("Top 10 contracts. Full calendar on the Marketplace page.")

st.subheader("Combined — Services + Marketplace (base)")
show(ch.combined_chart(ny, budgets), width="stretch")

# ----------------------------------------------------------------------------- 6. drivers
st.subheader("What is driving the growth?")
d1, d2 = st.columns(2)
for col, p in ((d1, "AWS"), (d2, "GCP")):
    with col:
        st.markdown(f"**{p}** — services by trend ($ added per month, per month)")
        st.dataframe(nys.drivers_display(ny, p), width="stretch")

# ----------------------------------------------------------------------------- 7. accounts / projects
st.subheader("By account and by project (bottom-up)")
st.caption(nys.ACCOUNT_CAPTION)
ta, tg = st.tabs(["AWS accounts", "GCP projects"])
for tab, p in ((ta, "AWS"), (tg, "GCP")):
    with tab:
        t = ny.tables[p]
        fyc = f"FY{fy} forecast"
        st.markdown(nys.account_text(ny, sm, p))
        show(ch.accounts_share(t, ny, p), width="stretch")
        st.dataframe(nys.account_display(ny, p), width="stretch")
        st.download_button("Download CSV", t.round(2).to_csv().encode(), f"{p}_FY{fy}_forecast_by_line.csv", "text/csv", key=f"dl_{p}")
        with st.expander("Small multiples — every line, actual vs forecast"):
            show(ch.small_multiples(ny, p), width="stretch")
        with st.expander("Services inside one account / project"):
            pick = st.selectbox("Choose", [x for x in t.index if not x.startswith("All other")], key=f"pick_{p}")
            st.dataframe(ny_mod.account_services(df_all, ny, p, pick).style.format(
                {"Last 12 mo": money, "Exit run-rate /mo": money, fyc: money}), width="stretch")

st.markdown("**Top-down vs bottom-up reconciliation**")
show(ch.reconciliation(ny, budgets), width="stretch")

# ----------------------------------------------------------------------------- 7b. look-back
st.subheader("How did this method do last year?")


@st.cache_data(show_spinner="Re-running last year's forecast…")
def _lookback(stamp: float):
    return hist.lookback(get_data(apply_view=False))


lb = _lookback(float(len(df_all)))
if lb is None:
    st.info("A look-back test needs at least 24 billed months of history.")
else:
    for t in hist.lookback_text(lb):
        st.markdown(f"- {t}")
    show(hist.fig_lookback(lb), width="stretch")
    st.dataframe(hist.lookback_table(lb), hide_index=True, width="stretch")
    st.caption("Cloud Services only, no reviewed adjustments (those were made with hindsight). Use it to judge how wide the Low–High range needs to be.")

# ----------------------------------------------------------------------------- 8. anomalies
an = nys.anomalies_display(ny)
if an is not None:
    st.subheader("Data anomalies — review status")
    st.caption(nys.ANOMALY_CAPTION)
    for line in nys.adjustments_lines(ny):
        st.markdown(f"- {line}")
    st.dataframe(an, hide_index=True, width="stretch")
st.subheader("Review anomalies")
with st.expander("Record or change a review (saves to data/anomaly_rules.csv)", expanded=bool(an is not None and (an["Status"] == "Pending review").any())):
    editor_ui.rules_editor(ny.anomalies)

with st.expander("Method & caveats"):
    st.markdown(nys.METHOD_MD)

# ----------------------------------------------------------------------------- HTML report
st.divider()
st.subheader("HTML report")
st.caption("Self-contained, print-ready (light theme, landscape Letter). Uses the **budgets and number of modelled lines set in the sidebar**; "
           "tables show the Base scenario. Open it in a browser and use *Print → Save as PDF* for a PDF.")
if st.button("Build HTML report", key="ny_report_btn"):
    with st.spinner("Building report…"):
        html = build_report(ny, budgets, assumed, lb)
        path = save_report(html, ny)
    st.session_state["ny_report"] = (html, path.name, str(path))
if "ny_report" in st.session_state:
    html, name, path = st.session_state["ny_report"]
    st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key="ny_report_dl")
    st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")
