import pandas as pd
import streamlit as st
from finops import equinix as eq
from finops import platforms
from finops.ui import money, show

st.title("Equinix")
st.caption("Equinix Fabric and Network Edge, billed separately from AWS and GCP. It is **not** part of the cloud totals, budgets or savings. "
           "Charges are before tax; credits are one-time refunds and are shown on their own.")


@st.cache_data(show_spinner=False)
def _load(stamp: str):
    return eq.read()


lines, inv = _load(platforms.last_refresh(eq.PLATFORM))
d = eq.summarize(lines, inv)
if d is None:
    st.info("No Equinix data yet. Go to **Data Health → Refresh Equinix** (needs `EQUINIX_CLIENT_ID` / `EQUINIX_CLIENT_SECRET` in your env file; see the README).")
    st.stop()

st.caption(f"Latest invoice month: **{d['last']:%B %Y}** · {d['months']} months of history · data last refreshed {platforms.last_refresh(eq.PLATFORM)}.")
for col, it in zip(st.columns(5), eq.kpi_items(d)):
    col.metric(it["label"], it["value"], it["delta"] or None, delta_color="inverse" if it.get("inverse") else "off", help=it["help"] or None)

items = eq.latest_items(lines, d["last"])
chg = eq.changes(lines, d["last"], d["prev"])
for t in eq.findings(d, items, chg):
    st.markdown(f"- {t}")

st.subheader("Monthly charges by category")
show(eq.fig_monthly(d), width="stretch")

a, b = st.columns([3, 2])
with a:
    st.subheader(f"{d['last']:%B %Y} by item")
    show(eq.fig_items(items), width="stretch")
with b:
    st.subheader("Table")
    st.dataframe(eq.items_display(items), hide_index=True, width="stretch", height=380)

if len(chg):
    st.subheader(f"What changed since {d['prev']:%B %Y}")
    st.dataframe(eq.changes_display(chg), hide_index=True, width="stretch")
else:
    st.caption(f"No line changed between {d['prev']:%B} and {d['last']:%B %Y}." if pd.notna(d["prev"]) else "")

fc = eq.fig_credits(d)
if fc is not None:
    st.subheader("Credits")
    show(fc, width="stretch")
    st.caption("One-time refunds or adjustments. They are never counted as lower charges, run-rate reductions or savings.")

with st.expander("Monthly table and download"):
    mt = eq.monthly_display(d)
    st.dataframe(mt.style.format(lambda v: money(v) if pd.notna(v) else "–"), width="stretch")
    st.download_button("Download lines CSV", lines.to_csv(index=False).encode(), "equinix_invoice_lines.csv", "text/csv")

with st.expander("Method & caveats"):
    st.markdown(
        "- **Source:** Equinix Billing API (`/v2/invoices` and `/v2/invoices/details`), read-only. Equinix keeps about 12 months, so each refresh **adds** to the history saved here.\n"
        "- **Charges** = recurring + one-time amounts on invoice lines, before tax. **Credits** are negative lines or credit memos and are kept apart.\n"
        "- **Run-rate** is the recurring total on the latest invoice.\n"
        "- **Names:** lines are matched to connections, Cloud Routers and Network Edge nodes through the UUID in the line. Equinix truncates some names.\n"
        "- Utilization, right-sizing and BGP reliability are in the standalone Equinix report; they need the Equinix SDK and are not part of this page.")

st.divider()
st.subheader("HTML report")
if st.button("Build HTML report", key="eq_report_btn"):
    from finops.equinix_report import build_report, save_report
    with st.spinner("Building report…"):
        html = build_report(lines, inv)
        path = save_report(html, d["last"])
    st.session_state["eq_report"] = (html, path.name, str(path))
if "eq_report" in st.session_state:
    html, name, path = st.session_state["eq_report"]
    st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key="eq_report_dl")
    st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")
