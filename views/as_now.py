import pandas as pd
import streamlit as st
from finops import asnow
from finops.asnow_charts import budget_chart, movers_chart
from finops.ui import get_data, money, pct, show

st.title("Month to Date")
a = asnow.compute(get_data(include_partial=True, apply_view=False))
if a is None:
    st.info("No in-progress month in the data yet. Open **Data Health** and click Refresh (AWS and GCP) — a refresh pulls the current month as 'as-now'.")
    st.stop()

T, P = a.total, a.prov
st.caption(f"**{a.month:%B %Y}** · data through **{a.as_of:%b %d}** (last complete day) · day {a.n_days} of {a.month_days} · "
           + " · ".join(f"{p} refreshed {a.refreshed[p]:%b %d %H:%M}" for p in ("AWS", "GCP") if a.refreshed.get(p) is not None))
if a.stale:
    st.warning("The last refresh was in an earlier month, so this is not a live month-to-date. Refresh on the Data Health page.")
if a.n_days < 10:
    st.warning(f"Confidence: **{a.confidence}**. Projections swing a lot early in the month; the band below narrows each day.")

# ----------------------------------------------------------------------------- KPIs
vb = T["projected"] - T["budget"]
k = st.columns(5)
k[0].metric(f"{a.month:%b} Cloud Services so far", money(T["mtd"]), help="AWS + GCP Cloud Services, billed through the last complete day")
k[1].metric("Projected month-end", money(T["projected"]), f"{money(T['low'])} – {money(T['high'])}", delta_color="off",
            help=f"Band = ±{T['band']:.0%}, narrowing through the month")
k[2].metric("vs monthly budget", (money(abs(vb)) + (" over" if vb > 0 else " under")) if T["budget"] else "n/a",
            pct(T["projected"] / T["budget"] - 1) if T["budget"] else None, delta_color="inverse", help=f"Budget {money(T['budget'])}/month (AWS + GCP Cloud Services)")
k[3].metric(f"vs {a.prev:%B}", pct(T["projected"] / T["last"] - 1) if T["last"] else "–", f"{a.prev:%b}: {money(T['last'])}", delta_color="off")
k[4].metric("Marketplace (projected)", money(T["mkt_projected"]), f"{money(T['mkt_mtd'])} billed + {money(T['mkt_remaining'])} scheduled", delta_color="off",
            help="Outside the budget. Scheduled charges come from the contract renewal calendar")

if T["budget"]:
    used, el = T["mtd"] / T["budget"], a.frac
    st.progress(min(used, 1.0), text=f"Budget used so far: {used:.0%} of {money(T['budget'])} · month elapsed: {el:.0%}"
                                    + (" — lump charges (support, reserved fees) post early, so this overstates the pace" if used > el * 1.15 else ""))
for p in ("AWS", "GCP"):
    r = P[p]
    if r["budget"]:
        gap = r["projected"] - r["budget"]
        st.markdown(f"- **{p}:** {money(r['mtd'])} so far → projected **{money(r['projected'])}** ({money(r['low'])}–{money(r['high'])}); "
                    f"budget {money(r['budget'])} → **{money(abs(gap))} {'over' if gap > 0 else 'under'}**; {pct(r['projected'] / r['last'] - 1)} vs {a.prev:%b}.")
st.markdown(f"- **Tax (separate):** about {money(T['tax_projected'])} projected for the month.")

# ----------------------------------------------------------------------------- charts
c1, c2 = st.columns([3, 4])
show(budget_chart(a), container=c1, width="stretch")
with c2:
    level = st.radio("Biggest changes by", ["service", "account"], horizontal=True, format_func=lambda x: "Service" if x == "service" else "Account / Project")
    show(movers_chart(a, level), container=c2, width="stretch")

# ----------------------------------------------------------------------------- watch lists
st.subheader("Where the projection needed judgement")
st.caption("A plain run-rate (so far ÷ days × days in month) would give "
           f"**{money(T['naive'])}**. Charges that post as lumps are handled separately; these lines drive the difference.")
ls = asnow.lump_summary(a)
if len(ls):
    t = ls.head(12)
    st.dataframe(pd.DataFrame({"provider": t.provider, "account / project": t.account, "service": t.service, "so far": t.mtd.map(money),
                               f"{a.prev:%b}": t["last"].map(money), "projected": t.projected.map(money), "treated as": t.method,
                               "vs plain run-rate": t.effect_vs_naive.map(lambda v: ("+" if v > 0 else "−") + money(abs(v)))}),
                 hide_index=True, width="stretch")
    st.caption(f"{len(ls)} lines treated specially. **Already billed** = reached ≥85% of last month's amount early in the month, so not extrapolated. "
               "**Not posted yet** = billed last month, nothing yet this month (assumed the same). **Ahead of pace** = would exceed 1.6× last month, capped at 1.5×.")
else:
    st.success("No lines needed special treatment.")

if len(a.mkt_detail):
    st.subheader("Marketplace charges scheduled this month")
    md = a.mkt_detail.sort_values("still expected", ascending=False)
    st.dataframe(md.assign(scheduled=md.scheduled.map(money), **{"billed so far": md["billed so far"].map(money), "still expected": md["still expected"].map(money)}),
                 hide_index=True, width="stretch")
    st.caption("From the contract renewal calendar. A scheduled charge can post later than the day shown; confirm large ones (e.g. an annual renewal) with the owner.")

with st.expander("How the projection works"):
    st.markdown(f"""
- **Cloud Services** is projected line by line (provider × account × service): *continuous* usage = so far ÷ share of month elapsed; *already billed* lumps
  are not extrapolated; *not posted yet* charges are assumed equal to last month; lines running far ahead of pace are capped at 1.5× last month.
- **Band:** ±30% × the share of the month still to come (never below ±5%). It is a rule of thumb, not a statistical interval.
- **Marketplace:** billed so far + scheduled-but-not-yet-billed charges from the contract calendar. **Tax:** observed 12-month rate × (services + marketplace).
- **Data:** the refresh pulls the in-progress month with the rest. "Last complete day" is the day before your oldest provider refresh.
- Billed history, forecasts and reports **never include** the in-progress month; it lives only on this page and the Executive Summary strip.
""")
