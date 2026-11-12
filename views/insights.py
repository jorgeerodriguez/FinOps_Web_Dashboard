import pandas as pd
import streamlit as st
from finops import dims as D
from finops import history as H
from finops import opportunities as O
from finops import savings as S
from finops.insights import (add_notes, anomalies, new_spend, ownership_gap, spikes, structure_flags, sustained_growth,
                             top_movers)
from finops.ui import current_view, get_data, money, require_data, show as show_fig, sidebar_filters

st.title("Insights & Savings")
df = sidebar_filters(get_data())
require_data(df)

st.caption("Rule-based and explainable, built on the full history. **Savings** are measured against each line's own earlier peak; "
           "**spend at stake** elsewhere is the amount in question, not a promised saving. Every item needs its owner's confirmation: "
           "this is cost data, so it shows *what* changed, not *why*.")
if current_view() != "Cloud Services (budget basis)":
    st.warning(f"These checks are designed for Cloud Services; you're viewing **{current_view()}**. "
               "For Marketplace use the Marketplace page.")

months = sorted(df.date.unique())
month = pd.Timestamp(st.sidebar.selectbox("Month (for the month-based tabs)", months[::-1], format_func=lambda d: pd.Timestamp(d).strftime("%b %Y")))
min_amt = st.sidebar.number_input("Materiality ($ per month)", 500, 100000, 5000, 500,
                                  help="Ignore items smaller than this monthly amount. Also the smallest step-down counted as a saving.")
level_name = st.sidebar.radio("Look at", ["Account / Project", "Service", "Project code"],
                              help="Project code treats everything with the same code as ONE line across AWS and GCP, so spend moved between its accounts (or between clouds) nets out instead of reading as a saving here and new spend there.")
level = {"Account / Project": "account", "Service": "service", "Project code": "project_code"}[level_name]
lens = {"account": "account / project", "service": "service", "project_code": "project code"}[level]
st.sidebar.caption("The savings ledger compares each line with its own peak over the whole history. Accounts, services and project codes overlap (they are different cuts of the same dollars), so don't add the lenses together.")

h = H.compute(df)
sc = S.annotate(S.scan(S.adjusted_series(df, level), min_amt, S.owners_map(df, level)), df, level)
acct = sc if level == "account" else S.annotate(S.scan(S.adjusted_series(df, "account"), min_amt, S.owners_map(df, "account")), df, "account")

for col, it in zip(st.columns(5), S.kpis(sc, h)):
    col.metric(it["label"], it["value"], it["delta"] or None, delta_color="off", help=it.get("help") or None)

t_led, t_own, t_opp, t_spk, t_gro, t_str, t_mov = st.tabs(["Savings ledger", "Owners & projects", "Opportunities", "Spikes & new spend", "Creeping growth",
                                                          "Structure & Finance gaps", "Movers & anomalies"])
flags = structure_flags(df, month)
sp = add_notes(spikes(df, level, month, min_amt), df, level)
nw = add_notes(new_spend(df, level, month, min_amt), df, level)
gr = add_notes(sustained_growth(df, level, month, min_amt * 0.6), df, level)
og = ownership_gap(df, month)


def table(t: pd.DataFrame, money_cols=(), pct_cols=()):
    if t.empty:
        st.success("Nothing found at this threshold.")
        return
    st.dataframe(t.assign(**{c: t[c].map(money) for c in money_cols},
                          **{c: t[c].map(lambda v: "–" if pd.isna(v) else f"{v:+.0%}") for c in pct_cols}),
                 hide_index=True, width="stretch")


# ----------------------------------------------------------------------------- 1. savings ledger
with t_led:
    st.subheader("What has actually come down")
    for t in S.narrative(sc, h, lens):
        st.markdown(f"- {t}")
    if sc.ledger.empty:
        st.info("No line has stepped down by the materiality amount and stayed down for 3 months.")
    a, b = st.columns(2)
    a.markdown("**Avoided cost per month** (versus each line's old peak level)")
    show_fig(S.fig_over_time(sc), container=a, width="stretch")
    b.markdown("**Biggest step-downs** (lower per month than at peak)")
    show_fig(S.fig_top(sc), container=b, width="stretch")
    st.caption("Green = reduced line (a saving). Grey = ended line: confirm the shutdown was intended before counting it.")

    st.subheader("The whole-provider picture")
    for t in S.arc_lines(df):
        st.markdown(f"- {t}")
    show_fig(S.fig_arc(df), width="stretch")
    st.caption("3-month average of each provider's total. Circle = peak, diamond = low point, dark dot = now.")

    st.subheader("Where the savings landed")
    st.caption("Reduced lines only, credited to the project code, environment and cost center of each account / project (account-level step-downs). A line with no project code or environment shows as missing: fix it in Data Health → Owners.")
    c1, c2, c3 = st.columns(3)
    for col, by, title in ((c1, "project_code", "By project code"), (c2, "environment", "By environment"), (c3, "cost_center", "By cost center")):
        t_ = S.landed(acct.ledger, by)
        if len(t_):
            show_fig(S.fig_landed(t_, title, 8), container=col, width="stretch")
        else:
            col.info(f"No reduced lines to show {title.lower()}.")

    st.subheader(f"Ledger — by {lens}")
    kinds = st.multiselect("Show", list(S.CLASS_HELP), default=["Reduced", "Ended"], help="\n".join(f"{k}: {v}" for k, v in S.CLASS_HELP.items()))
    led = sc.ledger[sc.ledger.cls.isin(kinds)]
    if level == "account" and len(sc.ledger):
        f1, f2 = st.columns(2)
        codes = f1.multiselect("Project code", sorted(set(sc.ledger.project_code)), key="led_code")
        envs = f2.multiselect("Environment", sorted(set(sc.ledger.environment)), key="led_env")
        if codes:
            led = led[led.project_code.isin(codes)]
        if envs:
            led = led[led.environment.isin(envs)]
    st.dataframe(S.ledger_display(led), hide_index=True, width="stretch")
    st.download_button("Download ledger CSV", sc.ledger.round(2).to_csv(index=False).encode(), f"savings_ledger_by_{level}.csv", "text/csv", key="led_dl")
    if len(sc.erosion):
        st.subheader("Savings that came back")
        st.caption("Lines that fell and then climbed again. “Above old peak” means the line now costs more than before it was reduced.")
        st.dataframe(S.erosion_display(sc.erosion), hide_index=True, width="stretch")
    with st.expander("How a saving is counted"):
        st.markdown("""
A line (an account, project or service) is compared with **its own highest 3-month average** (the *peak*), measured at least 3 months before the latest billed month.

- **Qualifies** when the latest 3-month average is at least the materiality amount lower, is at most 85% of the peak, and none of the latest 3 months is back near the peak.
- **Reduced** = still running, smaller → counted as a saving. **Ended** = under 10% of the peak → a shutdown, listed but only a saving once confirmed.
- **One-off** = a single month carried 70%+ of the peak window → never recurring cost, **not** a saving. **Check** = some (not all) recent months are $0 → likely billing timing, **not** a saving.
- **Realized to date** = (peak level − actual) summed over every month since the step-down. **Per year** = the monthly drop × 12.
- **Savings given back** = the part of an earlier reduction that has returned since the low point.
- **Billing gaps are added back first**: the `add_back` rows you reviewed in `data/anomaly_rules.csv`, and Savings Plans fee-timing gaps, each filled from the line's neighbouring months, so a bill that did not arrive is never mistaken for a saving (or for savings coming back).
- The Savings Plans service itself is not listed as a line (billed in lumps with gaps). Cost data cannot say *why* a line fell: migration to the other cloud, a rightsizing, a contract change or a shutdown all look the same.
""")

# ----------------------------------------------------------------------------- 2. owners and projects
with t_own:
    by = {"Owner": "owner", "Project code": "project_code", "Cost center": "cost_center"}[st.radio("Group by", ["Owner", "Project code", "Cost center"], horizontal=True, key="sc_by",
        help="The same scorecard for whoever you need to talk to: the owner, the project (Finance's view) or the cost center. Savings are credited to the group each account / project belongs to.")]
    blabel, bmissing = S.BY_LABEL[by], S.BY_MISSING[by]
    sc_t = S.owner_scorecard(df, h, acct, by)
    st.subheader(f"{blabel} scorecard")
    st.caption(f"Latest 12 months ({h.cur[0]:%b %Y} – {h.last:%b %Y}) vs the 12 before. Savings are the account / project step-downs, grouped by {blabel.lower()}. "
               "Values come from `data/owners_aws.csv` and `data/owners_gcp.csv`.")
    ua = sc_t.loc[bmissing] if bmissing in sc_t.index else None
    if ua is not None and ua.latest > 0:
        st.warning(f"**{money(ua.latest)}** over the last 12 months sits in {int(ua.accounts)} accounts / projects with no {blabel.lower()}. Fill them in **Data Health → Owners**.")
    st.dataframe(S.scorecard_display(sc_t), hide_index=True, width="stretch")
    st.download_button("Download scorecard CSV", sc_t.round(2).to_csv().encode(), f"scorecard_by_{by}.csv", "text/csv", key="own_dl")
    show_fig(S.fig_owner_change(sc_t), width="stretch")
    st.caption("Change in spend vs the prior 12 months. Red = spend went up, green = went down.")

    st.subheader(f"Review one {blabel.lower()}")
    named = [o for o in sc_t.index if o != bmissing] + ([bmissing] if bmissing in sc_t.index else [])
    who = st.selectbox(blabel, named, key=f"sc_who_{by}")
    d = S.owner_detail(df, h, who, acct, by=by)
    ch = d["latest"] - d["prior"]
    k = st.columns(4)
    k[0].metric("Latest 12 months", money(d["latest"]), (("+" if ch > 0 else "") + money(ch)) + " vs prior", delta_color="inverse")
    k[1].metric("Savings run-rate", money(d["ledger"][d["ledger"].cls == "Reduced"]["lower"].sum()) + "/mo", delta_color="off")
    k[2].metric("Realized to date", money(d["ledger"][d["ledger"].cls == "Reduced"].realized.sum()), delta_color="off")
    k[3].metric("Given back", money(d["erosion"].regrown.sum()) + "/mo", delta_color="off")
    if d["notes"]:
        st.markdown("**What it is (finance notes):** " + " · ".join(d["notes"][:6]))
    show_fig(d["fig"], width="stretch")
    at = d["accounts"].assign(latest_=d["accounts"].latest.map(money), prior_=d["accounts"].prior.map(money),
                              change_=d["accounts"].change.map(lambda v: ("+" if v > 0 else "") + money(v)),
                              pct_=d["accounts"].pct.map(lambda v: "new" if pd.isna(v) else (f"{1 + v:.0f}×" if v >= 3 else f"{v:+.0%}")))
    st.markdown("**Accounts and projects**")
    st.dataframe(at[["provider", "name", "prior_", "latest_", "change_", "pct_"]].rename(columns={"provider": "Provider", "name": "Name", "prior_": "Prior 12 mo",
                 "latest_": "Latest 12 mo", "change_": "Change", "pct_": "%"}), hide_index=True, width="stretch")
    if len(d["ledger"]):
        st.markdown("**Step-downs credited here**")
        st.dataframe(S.ledger_display(d["ledger"]).drop(columns=["Owner"]), hide_index=True, width="stretch")
    if len(d["erosion"]):
        st.markdown("**Savings that came back**")
        st.dataframe(S.erosion_display(d["erosion"]).drop(columns=["Owner"]), hide_index=True, width="stretch")
    rising = d["accounts"][d["accounts"].change > 0].sort_values("change", ascending=False).head(5)
    if len(rising):
        st.markdown("**Biggest increases to ask about:** " + "; ".join(
            f"{r.provider} · {r.name[:30]} ({'+' + money(r.change)}{', new' if r.prior < 1000 else ''})" for r in rising.itertuples()))
    st.download_button(f"Download this {blabel.lower()}'s accounts CSV", d["accounts"].round(2).to_csv(index=False).encode(), f"{by}_{who.replace(' ', '_')}.csv", "text/csv", key="own_one_dl")

    st.markdown("**Printable review pack**")
    st.caption(f"A self-contained HTML page for **{who}**: costs, what changed, savings, what came back, finance notes and open gaps. Always on the Cloud Services budget basis. "
               "Open it in a browser and use *Print → Save as PDF*.")
    if st.button("Build review pack", key="review_btn"):
        from finops.review_report import build_review, save_review
        with st.spinner("Building review pack…"):
            html = build_review(by, who, min_amt)
            path = save_review(html, by, who)
        st.session_state["review_pack"] = (html, path.name, str(path))
    if "review_pack" in st.session_state:
        html, name, path = st.session_state["review_pack"]
        st.download_button(f"Download {name}", html.encode("utf-8"), name, "text/html", key="review_dl")
        st.caption(f"Also saved to `{path}` ({len(html) / 1e6:.1f} MB)")

# ----------------------------------------------------------------------------- 3. opportunities
with t_opp:
    st.caption("**Leads, not savings.** Each item is spend worth a conversation with its owner. Cost data cannot see utilization, existing reservations or why something runs, "
               "so nothing here is a promised saving. Thresholds follow the materiality setting in the sidebar.")
    npv = O.nonprod_heavy(df, min_amt * 4)
    stl = O.steady_lines(df, min_amt)
    crt = O.credits_watch(df)
    k = st.columns(3)
    k[0].metric("Non-prod as big as prod", f"{len(npv)} projects", money(npv["non-prod"].sum()) + " non-prod / 12 mo" if len(npv) else None, delta_color="off")
    k[1].metric("Steady always-on lines", f"{len(stl)} lines", money(stl.annual.sum()) + " / 12 mo" if len(stl) else None, delta_color="off")
    k[2].metric("Credits last month", money(crt.sort_values("date").groupby("provider").credits.last().sum()) if len(crt) else "–", delta_color="off")

    st.subheader("Non-prod as big as prod")
    st.caption("Project codes whose non-prod spend is at least their prod spend over the latest 12 months. **Unspecified** spend may be prod, so a project with a lot of it is a weaker lead "
               "(fill the environment in Data Health to sharpen this). Typical questions: can it run on a schedule, be smaller, or be turned off when idle?")
    if len(npv):
        st.dataframe(O.nonprod_display(npv), hide_index=True, width="stretch")
    else:
        st.success("No project has non-prod spend at least as large as prod at this threshold.")

    st.subheader("Steady always-on lines")
    st.caption("Lines that billed every one of the last 12 months, at least the materiality amount on average, varying under 8%. Always-on spend is what commitment discounts fit. "
               "Billing data does not show whether a reservation or Savings Plan already covers a line: check coverage in the cloud console before acting. Support, Savings Plans and tax are left out.")
    if len(stl):
        st.dataframe(O.steady_display(stl), hide_index=True, width="stretch")
    else:
        st.info("No line is steady enough at this threshold.")

    st.subheader("Credits")
    if len(crt):
        for line in O.credits_lines(crt):
            st.markdown(f"- {line}")
        show_fig(O.fig_credits(crt), width="stretch")
    else:
        st.info("No credits in the billing data.")

# ----------------------------------------------------------------------------- 3-6. existing detectors
with t_spk:
    st.subheader(f"Spikes vs trailing 3-month average ({level_name.lower()}) — {month:%b %Y}")
    table(sp, ["trailing_3mo_avg", "this_month", "increase"], ["increase_pct"])
    st.subheader("New spend (nothing in the prior 3 months)")
    table(nw, ["prior_3mo_max", "this_month"])
with t_gro:
    st.subheader(f"Last 3 months vs the 3 before ({level_name.lower()})")
    table(gr, ["prior_3mo_avg", "last_3mo_avg", "increase_per_month", "annualized"], ["increase_pct"])
with t_str:
    st.subheader(f"Cost-structure checks — {month:%b %Y}")
    if flags.empty:
        st.success("No structural flags.")
    for r in flags.itertuples():
        box = {"High": st.error, "Medium": st.warning}.get(r.severity, st.info)
        box(f"**{r.finding}** · {money(r.spend_at_stake)} at stake\n\n{r.evidence}")
    st.subheader("Finance gaps — what is still missing in the owner files")
    st.caption("Share of the latest 12 months' spend whose account / project has each value. Blank and placeholder values count as missing. Fix them in **Data Health → Owners** "
               "(its *Show* filter lists exactly these rows) and every chart and report picks the change up.")
    st.dataframe(D.readiness_display(df), hide_index=True, width="stretch")
    g1, g2, g3 = st.columns(3)
    for col, field in ((g1, "environment"), (g2, "project_code"), (g3, "cost_center")):
        cv = D.coverage(df, field)
        with col:
            st.metric(f"{D.LABEL[field]} missing on", money(cv["missing"]), f"{1 - cv['share']:.0%} of spend", delta_color="off")
            if len(cv["worst"]):
                st.dataframe(cv["worst"].assign(**{"Spend without it": cv["worst"]["Spend without it"].map(money)}).head(10), hide_index=True, width="stretch")
    st.subheader("Accounts / projects with no owner")
    st.caption("Fix by filling `owner` in Data Health → Owners (or data/owners_aws.csv / data/owners_gcp.csv).")
    table(og, ["this_month", "last_12mo"])
with t_mov:
    for lv in ("service", "account", "project_code"):
        st.subheader(f"Top movers by {lv.replace('_', ' ')} — {month:%b %Y} vs prior month")
        t = top_movers(df, lv, month)
        st.dataframe(t.assign(previous=t.previous.map(money), current=t.current.map(money), change=t.change.map(money),
                              change_pct=t.change_pct.map(lambda v: f"{v:+.0%}" if pd.notna(v) else "new")),
                     hide_index=True, width="stretch")
    st.subheader("Anomalous months")
    st.caption("Whole-provider months deviating > 2σ from the trailing 6-month mean. The Two-Year History page lists one-off months with their drivers.")
    for p, g in df.groupby("provider"):
        a = anomalies(g.groupby("date")["cost_net"].sum())
        st.markdown(f"**{p}**")
        if len(a):
            st.dataframe(a.assign(cost=a.cost.map(money), trailing_mean=a.trailing_mean.map(money), z=a.z.round(1)),
                         hide_index=True, width="stretch")
        else:
            st.write("None")
