import streamlit as st
from finops import marketplace_view as mv
from finops.marketplace_report import build_report, save_report
from finops.ui import get_data, show

st.title("Marketplace & Third-Party Contracts")
st.caption("Marketplace is lumpy (annual / quarterly commitments), so it's planned as a **renewal calendar**: each "
           "contract's cadence is read from its last 12 billed months and repeated forward; the 12 months before "
           "show whether each contract's price moved at renewal. Not part of the Cloud Services budget.")

allc = get_data(apply_view=False)
provs = st.sidebar.multiselect("Provider", ["AWS", "GCP"], default=["AWS", "GCP"])
mkt = allc[(allc.category == "Marketplace") & allc.provider.isin(provs)]
if mkt.empty:
    st.info("No Marketplace data for this selection.")
    st.stop()

group = st.sidebar.radio("Rows by", ["Vendor", "Contract"])
topn = st.sidebar.slider("Max rows in calendar", 10, 60, 30)
nmonths = mkt.date.nunique()
hist = st.sidebar.slider("Billed months shown", 6, max(nmonths, 7), min(15, max(nmonths, 7)),
                         help="How many past months the calendar and cash view draw. Totals and the 12-vs-12 contract comparison always use everything loaded.")
d = mv.assemble(mkt, group, topn, hist)
yr = d["yr"]

cols = st.columns(5)
for col, it in zip(cols, d["kpis"]):
    col.metric(it["label"], it["value"], it["delta"] or None)

st.subheader("Billing calendar")
show(d["cal"], width="stretch")
st.caption(d["cal_caption"])

st.subheader("Monthly cash view")
show(d["bars"], width="stretch")

a, c = st.columns([1, 2])
with a:
    st.subheader(f"{yr + 1} by quarter")
    from finops.ui import money
    st.dataframe(d["quarter"].map(money).rename("Scheduled"), width="stretch")
with c:
    st.subheader("Contracts")
    st.dataframe(mv.contracts_display(d["contracts"], yr), hide_index=True, width="stretch")
    st.download_button("Download contracts CSV", d["contracts"].to_csv(index=False).encode(), "marketplace_contracts.csv", "text/csv")

if len(d["risk"]):
    st.subheader("Renewal watch — billed earlier, not in the last 12 months")
    st.caption("Ended, or renewing outside the window. Confirm with the owner before budgeting.")
    st.dataframe(mv.risk_display(d["risk"]), hide_index=True, width="stretch")

# ----------------------------------------------------------------------------- HTML report
st.divider()
st.subheader("HTML report")
st.caption("Self-contained, print-ready (light theme, landscape Letter). Covers AWS + GCP Marketplace regardless of the provider filter above. "
           "Open it in a browser and use *Print → Save as PDF* for a PDF.")
if st.button("Build HTML report", key="mk_report_btn"):
    with st.spinner("Building report…"):
        html = build_report(group=group)
        path = save_report(html, d["last"])
    st.session_state["mk_report"] = (html, path.name, str(path))
if "mk_report" in st.session_state:
    html, name, path = st.session_state["mk_report"]
    st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key="mk_report_dl")
    st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")
