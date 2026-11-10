import streamlit as st
from finops import history as H
from finops.history_report import build_report, save_report
from finops.ui import current_view, get_data, require_data, show, sidebar_filters

st.title("Two-Year History")
st.sidebar.caption("Spend category is chosen in the sidebar; Marketplace and Tax are always summarised separately at the bottom.")


@st.cache_data(show_spinner="Re-running last year's forecast… a few seconds")
def _lookback(stamp: float):
    return H.lookback(get_data(include_partial=False, apply_view=False))


df = sidebar_filters(get_data())
require_data(df)
h = H.compute(df)
if not h.has_prev:
    st.warning(f"Only {len(h.m)} billed months are loaded ({h.first:%b %Y} – {h.last:%b %Y}); a 12-vs-12 comparison needs 24. "
               "Open **Data Health** and refresh (the refresh window is `history_months` in config.toml).")

st.caption(f"{h.first:%b %Y} – {h.last:%b %Y} · {len(h.m)} billed months · viewing **{current_view()}** · fiscal year Jan–Dec. "
           "Windows are the latest 12 billed months against the 12 before, so a part-year is never compared with a full one.")
for col, it in zip(st.columns(5), H.kpi_items(h)):
    col.metric(it["label"], it["value"], it["delta"] or None, delta_color="off" if it.get("neutral") else "normal", help=it.get("help") or None)
for t in H.narrative(h):
    st.markdown(f"- {t}")

st.subheader("Every billed month")
show(H.fig_trend(h), width="stretch")
st.caption("Bars = monthly spend by provider; black line = trailing 12-month average (smooths one-offs); red dashed = budget where one exists.")

st.subheader("Same month, year over year")
show(H.fig_yoy(h), width="stretch")
st.caption("One line per year. Where a line sits above last year's, that month cost more than the same month a year earlier.")

a, b = st.columns(2)
a.subheader("Annual run-rate")
show(H.fig_rolling(h), container=a, width="stretch")
a.caption("Each point = the cost of the 12 months ending there. It rises when the latest month costs more than the month it replaces.")
b.subheader("What explains the change")
fig = H.fig_bridge(h)
if fig is not None:
    show(fig, container=b, width="stretch")
    b.caption("Prior 12 months → latest 12 months, by service. Red = spend went up, green = went down.")

st.subheader("Biggest changes — latest 12 months vs the 12 before")
t1, t2, t3 = st.tabs(["By service", "By account / project", "One-off months"])
with t1:
    st.dataframe(H.movers_display(H.movers(h, "service", 25)), hide_index=True, width="stretch")
with t2:
    st.dataframe(H.movers_display(H.movers(h, "account", 25)), hide_index=True, width="stretch")
    st.caption("“New” = under $1K in the prior 12 months. Account / project names are as billed.")
with t3:
    sd = H.spikes_display(h)
    if sd.empty:
        st.success("No month stands out from its neighbours at this threshold.")
    else:
        st.dataframe(sd, hide_index=True, width="stretch")
        st.caption("A month is listed when it is ≥35% (and ≥$25K) away from the median of the 3 months either side. "
                   "Confirm with the owner whether each one is expected before treating it as a trend.")

st.subheader("Reported separately (not in budget)")
allc = get_data(apply_view=False)
st.dataframe(H.separate_summary(allc, h), hide_index=True, width="stretch")
show(H.fig_marketplace(allc, h), width="stretch")
st.caption("Marketplace is contract-driven and lumpy; its renewal calendar is on the Marketplace page.")

st.subheader("Would our forecast method have called it?")
lb = _lookback(float(len(allc)))
if lb is None:
    st.info("A look-back test needs at least 24 billed months.")
else:
    for t in H.lookback_text(lb):
        st.markdown(f"- {t}")
    show(H.fig_lookback(lb), width="stretch")
    st.dataframe(H.lookback_table(lb), hide_index=True, width="stretch")
    st.caption("Cloud Services only. The Next Year method was re-run using only the data that existed 12 months ago (no reviewed adjustments), "
               "then compared with what was billed. It is a measure of how much to trust the ranges, not a tuning target.")

st.subheader("Executive summary — what moved, in words")
st.caption("Latest 12 months vs the 12 before. Written from the same numbers as the tables above; a one-off or a shutdown is not counted as a saving.")
for _title, _lines in H.story_sections(h):
    st.markdown(f"#### {_title}")
    for _l in _lines:
        st.markdown(_l)

st.divider()
st.subheader("HTML report")
st.caption("Self-contained, print-ready (light theme, landscape Letter). Always built on the **Cloud Services budget basis** for AWS + GCP. "
           "Open it in a browser and use *Print → Save as PDF* for a PDF.")
if st.button("Build HTML report", key="hist_report_btn"):
    with st.spinner("Building report…"):
        html = build_report()
        path = save_report(html, h.last)
    st.session_state["hist_report"] = (html, path.name, str(path))
if "hist_report" in st.session_state:
    html, name, path = st.session_state["hist_report"]
    st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key="hist_report_dl")
    st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")
