import pandas as pd
import streamlit as st
from finops import grafana as gf
from finops import platforms
from finops.grafana import money
from finops.ui import show

st.title("Grafana Cloud")
st.caption("Grafana Cloud usage cost, billed separately from AWS and GCP. It is **not** part of the cloud totals, budgets or savings. "
           "Figures are **Grafana-reported usage cost, not invoices**; the month in progress is a running estimate.")


@st.cache_data(show_spinner=False)
def _load(stamp: str):
    return gf.read()


data = _load(platforms.last_refresh(gf.PLATFORM))
budget = float(platforms.section("grafana").get("monthly_budget", 0) or 0)
d = gf.summarize(data, budget)
if d is None:
    st.info("No Grafana data yet. Go to **Data Health → Refresh Grafana** (needs `GRAFANA_STACK_TOKEN` and `[grafana] url`; see the README).")
    st.stop()

st.caption(f"Last complete month: **{d['last']:%B %Y}** · {d['months']} months of history · data last refreshed {platforms.last_refresh(gf.PLATFORM)}.")
for col, it in zip(st.columns(5), gf.kpi_items(d)):
    col.metric(it["label"], it["value"], it["delta"] or None, delta_color="inverse" if it.get("inverse") else "off", help=it["help"] or None)

prod = gf.product_table(d)
for t in gf.findings(d, prod):
    st.markdown(f"- {t}")

st.subheader("Monthly cost by product")
show(gf.fig_monthly(d), width="stretch")
st.caption("The grey outlined bar is the month in progress (a running estimate)." + (" The dashed line is the monthly budget from config.toml." if budget else ""))

a, b = st.columns([3, 2])
with a:
    st.subheader(f"{d['last']:%B %Y} by product")
    st.dataframe(gf.product_display(prod, d["last"], d["prev"]), hide_index=True, width="stretch")
with b:
    fc = gf.fig_commit(d)
    if fc is not None:
        st.subheader("Prepaid commit")
        show(fc, width="stretch")
        cs = d["commit"]
        st.caption(f"{money(cs['balance'])} of {money(cs['total'])} remains. The dotted line continues at the recent burn rate.")

fd = gf.fig_drivers(d)
if fd is not None:
    st.subheader("What drives the cost (volume)")
    show(fd, width="stretch")
    st.caption("Monthly averages of the daily volume. Grafana bills metrics on the 95th percentile of active series, so a short spike matters less than a sustained rise.")

uc = gf.unit_costs(d)
if uc is not None:
    st.markdown("**Unit costs** — separates *we used more* from *it got more expensive*")
    st.dataframe(gf.unit_display(uc).style.format("${:,.2f}", na_rep="–"), width="stretch")
    note = gf.unit_note(d, uc)
    if note:
        st.markdown(f"- {note}")
    st.caption("Cost of a month's product divided by that month's average volume. The metrics row is approximate because metrics are billed on the 95th percentile.")

lv = gf.log_view(d["logs"])
if lv is not None:
    st.subheader("Logs by service")
    for t in gf.log_findings(lv):
        st.markdown(f"- {t}")
    c1, c2 = st.columns([2, 3])
    with c1:
        st.dataframe(gf.log_display(lv), hide_index=True, width="stretch", height=360)
    with c2:
        show(gf.fig_log_daily(lv), width="stretch")
    st.caption(f"Loki index volume for {lv['days']} days ({lv['first']:%b %d} to {lv['last']:%b %d}). Index volume is *stored* bytes, a fraction of what is billed, so use it for **relative shares and trends**, not dollars. "
               "Loki keeps only about 31 days, so each refresh adds days to what is saved here. Team labels cover under 1% of the volume, so this is by service, not by team.")
elif d["logs"] is not None and len(d["logs"]):
    st.caption(f"Logs by service needs at least {gf.MIN_LOG_DAYS} days of volume; refresh again later.")

av = gf.adaptive_view(d["adaptive"], d["facts"].get("active_series_now"))
if av is not None:
    st.subheader("Adaptive Metrics — largest reductions")
    pct = f" (**{av['pct_of_active']:.1%}** of the {d['facts']['active_series_now']:,.0f} active series)" if pd.notna(av["pct_of_active"]) else ""
    st.markdown(f"- Grafana's recommendations could remove about **{av['reducible']:,} series**{pct}; the top {len(av['top'])} metrics are {av['top_share']:.0%} of that. "
                f"Existing aggregation rules already cut these {av['metrics']:,} metrics from {av['raw']:,} raw series to {av['current']:,}, so what is left is small next to the whole.")
    if av["unused_series"]:
        st.markdown(f"- **{av['unused_series']:,} series** sit in metrics with no dashboard, rule or query using them: the safest ones to review first.")
    st.dataframe(gf.adaptive_display(av["top"]), hide_index=True, width="stretch")
    st.caption("A lead to review with the owners, not a confirmed saving: applying a recommendation changes which series exist. Metric and label names only; no sample data is read.")
elif d["facts"].get("recommended_savings_series"):
    sv, act_now = d["facts"]["recommended_savings_series"], d["facts"].get("active_series_now")
    st.caption(f"Grafana's own recommendations estimate about **{sv:,.0f} active series** could be saved" + (f" ({sv / act_now:.1%} of {act_now:,.0f})" if act_now else "")
               + ". A lead to review, not a confirmed saving.")

for n in d["facts"].get("notes") or []:
    st.caption(f"Not collected: {n}")

act = d["facts"].get("activity") or {}
if act.get("active_users") or act.get("folders_30d"):
    st.subheader("Who uses Grafana (activity, not cost)")
    c1, c2 = st.columns([1, 2])
    with c1:
        au = act.get("active_users") or {}
        for days in ("7", "30", "90"):
            if days in au:
                st.metric(f"Active users, {days} days", f"{au[days]:,}")
        if act.get("dashboards_total") is not None:
            used = act.get("dashboards_active_90d") or 0
            st.metric("Dashboards viewed in 90 days", f"{used:,} of {act['dashboards_total']:,}", f"{act['dashboards_total'] - used:,} not viewed", delta_color="off")
    with c2:
        fa = gf.fig_activity(act)
        if fa is not None:
            show(fa, width="stretch")
            st.caption("Dashboard query events per folder, last 30 days. Only counts are read; no user names or ids. Unviewed dashboards have no dollar cost, so no saving is claimed for them.")
    for n in act.get("notes") or []:
        st.caption(f"Not collected: {n}")

with st.expander("Does it reconcile? Reported cost vs the commit drawdown"):
    rec = d["reconciliation"]
    if len(rec):
        st.dataframe(gf.reconciliation_display(rec), hide_index=True, width="stretch")
        st.caption("The prepaid commit balance steps down shortly after each month closes by about that month's final cost. Differences under 2% are expected.")
    pv = d["parts_vs_total"]
    if len(pv):
        st.caption(f"Per-product costs add up to Grafana's own total in {int(pv['ok'].sum())} of {len(pv)} months.")

with st.expander("Monthly table and download"):
    st.dataframe(gf.monthly_display(d).style.format(lambda v: money(v) if pd.notna(v) else "–"), width="stretch")
    st.download_button("Download monthly cost CSV", d["monthly"].round(2).to_csv().encode(), "grafana_monthly_cost.csv", "text/csv")

with st.expander("Method & caveats"):
    st.markdown(
        "- **Source:** the stack's `grafanacloud-usage` datasource (read-only, through the Grafana datasource proxy) with a stack service-account token. No Cloud Access Policy token is needed.\n"
        "- **Cost:** `grafanacloud_org_*_overage` metrics are running USD amounts for the billing month; the last value of a month is its cost. They add up to `total_overage`.\n"
        "- **Not an invoice:** metric meanings are inferred from behaviour and checked against the commit drawdown above. Confirm once against a Cloud Portal invoice.\n"
        "- **History:** Grafana keeps about 400 days of these metrics; each refresh adds to what is saved here.\n"
        "- **No team attribution:** the usage metrics carry no team label, so cost cannot be split by team.")

st.divider()
st.subheader("HTML report")
if st.button("Build HTML report", key="gf_report_btn"):
    from finops.grafana_report import build_report, save_report
    with st.spinner("Building report…"):
        html = build_report(data, budget)
        path = save_report(html, d["last"])
    st.session_state["gf_report"] = (html, path.name, str(path))
if "gf_report" in st.session_state:
    html, name, path = st.session_state["gf_report"]
    st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key="gf_report_dl")
    st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")
