import streamlit as st
from finops import asnow
from finops import exec_summary as es
from finops.exec_charts import cumulative_block
from finops.exec_report import build_report, save_report
from finops.ui import get_data, money, show, sidebar_filters

st.title("Executive Summary")
st.sidebar.caption("Spend category is chosen in the sidebar; budgets apply to Cloud Services only.")
df = sidebar_filters(get_data())
k = es.compute(df)

st.caption(f"Billed data through **{k.last:%B %Y}** · fiscal year Jan–Dec")
cols = st.columns(5)
for col, it in zip(cols, es.kpi_items(k)):
    col.metric(it["label"], it["value"], it["delta"] or None, delta_color="inverse" if it.get("inverse") else "normal", help=it["help"] or None)

try:                                  # in-progress month: always Cloud Services (budget basis), AWS + GCP
    _a = asnow.compute(get_data(include_partial=True, apply_view=False))
except Exception:
    _a = None
if _a is not None and _a.total["budget"]:
    _T = _a.total
    _vb = _T["projected"] - _T["budget"]
    (st.warning if _vb > 0 else st.success)(
        f"**{_a.month:%B} so far** (through {_a.as_of:%b %d}): {money(_T['mtd'])} · projected month-end **{money(_T['projected'])}** "
        f"({money(_T['low'])}–{money(_T['high'])}) vs {money(_T['budget'])} budget → **{money(abs(_vb))} {'over' if _vb > 0 else 'under'}**. "
        f"Early-month estimate; details on **Month to Date**.")

left, right = st.columns(2)
left.subheader("Monthly spend by provider")
show(es.fig_provider_monthly(k), container=left, width="stretch")
right.subheader(f"{k.yr} by month vs budget")
fig, cap = es.fig_vs_budget(k)
show(fig, container=right, width="stretch")
right.caption(cap)

st.subheader(f"Cumulative spend vs budget — {k.yr}")
fig, verdict, cap = es.fig_cumulative(k)
show(fig, width="stretch")
if verdict:
    (st.error if verdict[0] == "error" else st.success)(verdict[1])
st.caption(cap)

st.subheader("Where the money goes — cumulative by service, account and project")
c1, c2 = st.columns(2)
topn = c1.slider("Entities shown per chart", 5, 12, 8, key="exec_topn")
other_by = c2.radio("Show everything else as", ["Single band", "Owner"], horizontal=True, key="exec_other",
                    help="Owner: split the grey remainder into bands per owner (needs the owner files filled in)")
base = get_data()                       # category view applied, all providers (independent of the provider filter)
t_aws_s, t_gcp_s, t_aws_a, t_gcp_p = st.tabs(["AWS Services", "GCP Services", "AWS Accounts", "GCP Projects"])
with t_aws_s:
    cumulative_block(base, "AWS", "service", "Service", topn, "aws_s", other_by)
with t_gcp_s:
    cumulative_block(base, "GCP", "service", "Service", topn, "gcp_s", other_by)
with t_aws_a:
    cumulative_block(base, "AWS", "account", "Account", topn, "aws_a", other_by)
with t_gcp_p:
    cumulative_block(base, "GCP", "account", "Project", topn, "gcp_p", other_by)

st.subheader("Reported separately (not in budget)")
st.dataframe(es.separate_table(get_data(apply_view=False), k), hide_index=True, width="stretch")

st.subheader("Top services (latest month)")
st.dataframe(es.top_services(k), hide_index=True, width="stretch")

# ----------------------------------------------------------------------------- HTML report
st.divider()
st.subheader("HTML report")
st.caption("A self-contained, print-ready page (light theme, landscape Letter). Always built on the **Cloud Services budget basis** "
           "for all providers, whatever the sidebar shows. Open it in a browser and use *Print → Save as PDF* for a PDF.")
if st.button("Build HTML report"):
    with st.spinner("Building report…"):
        html = build_report(topn=topn, other_by=other_by)
        path = save_report(html, k.last)
    st.session_state["exec_report"] = (html, path.name, str(path))
if "exec_report" in st.session_state:
    html, name, path = st.session_state["exec_report"]
    st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html")
    st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")
