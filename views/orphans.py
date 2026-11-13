import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from finops import audit as au
from finops import status as ST
from finops.config import load_config, resolve
from finops.ui import COLORS, TEMPLATE, get_data, money, show

ROOT = Path(__file__).resolve().parent.parent
st.title("Orphaned Accounts & Projects")
st.caption("Finds AWS accounts and GCP projects that look abandoned or unowned. **Screening** works now from the billing data. "
           "The **deep audit** adds access and activity checks and runs with *your own* credentials.")

df = get_data(apply_view=False)
if df.empty:
    st.info("No data yet — use Data Health → Refresh.")
    st.stop()


# ----------------------------------------------------------------------------------------- background deep audit
def _paths(p):
    d = au.audit_dir()
    return d / f"{p.lower()}_progress.log", d / f"{p.lower()}_run.pid"


def _running(p) -> bool:
    _, pidf = _paths(p)
    if not pidf.exists():
        return False
    try:
        os.kill(int(pidf.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def _launch(p, extra=()):
    log, pidf = _paths(p)
    f = open(log, "w")
    proc = subprocess.Popen([sys.executable, "-u", "-m", "ingest.audit", "--provider", p.lower(), *extra], cwd=ROOT,
                            stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    pidf.write_text(str(proc.pid))


@st.fragment(run_every="4s")
def _progress(p):
    log, _ = _paths(p)
    if _running(p):
        st.info(f"Deep audit is running… (started from this page; safe to leave it open or come back later)")
    if log.exists():
        lines = log.read_text().splitlines()
        if lines:
            st.code("\n".join(lines[-14:]))
            if not _running(p) and lines[-1].startswith("Done"):
                st.success("Finished — reload the page (or switch tabs) to see the new results.")


def _status_blocks(rows):
    a = au.audit_frame(rows)
    c = st.columns(5)
    for col, s in zip(c, ["spend", "idle", "gone", "review", "active"]):
        sub = a[a.status == s]
        col.metric(au.STATUS_LABEL[s], len(sub), money(sub.avg3.sum()) + "/mo" if len(sub) else None, delta_color="off")
    return a


def render(provider: str, noun: str):
    color = COLORS[provider]
    scr = au.screening(df, provider)
    # ------------------------------------------------------------------ screening
    st.subheader("1. Screening — from billing data (no credentials)")
    if scr.empty:
        st.info(f"No {provider} data.")
    else:
        orph = scr[scr.kind == "Orphan-like"]
        gap = scr[scr.kind == "Ownership gap"]
        k = st.columns(4)
        k[0].metric(f"{noun}s screened", len(scr))
        k[1].metric("Orphan-like", len(orph), money(orph.annualized_at_stake.sum()) + "/yr billing", delta_color="off",
                    help="Little or no spend: no spend in 12 months, dormant, near-zero, or spend collapsing")
        k[2].metric("Ownership gap", len(gap), money(gap.unowned_annual_spend.sum()) + "/yr unowned spend", delta_color="off",
                    help="Real spend with no owner in the owner file. Not a saving — a governance gap to close")
        k[3].metric("Owned & billing", int((scr.kind == "OK").sum()))
        kinds = st.multiselect("Show", ["Orphan-like", "Ownership gap", "OK"], default=["Orphan-like", "Ownership gap"], key=f"kinds_{provider}")
        view = scr[scr.kind.isin(kinds)]
        tbl_s = view[["account", "signal", "cost12", "avg3", "last_billed", "owner", "family", "top_service", "months_billed"]].copy()
        tbl_s.insert(1, "status", tbl_s["account"].map(df[df.provider == provider].drop_duplicates("account").set_index("account")["status"]).fillna("(not set)"))
        tbl_s["last_billed"] = pd.to_datetime(tbl_s["last_billed"]).dt.strftime("%b %Y")
        st.dataframe(tbl_s.assign(cost12=tbl_s.cost12.map(money), avg3=tbl_s.avg3.map(money)).rename(columns={
            "account": noun, "cost12": "last 12 mo", "avg3": "avg last 3 mo", "months_billed": "months billed (of 12)"}),
            hide_index=True, width="stretch")
        st.download_button("Download screening CSV", view.round(2).to_csv(index=False).encode(), f"{provider}_orphan_screening.csv", "text/csv", key=f"dls_{provider}")
        st.caption("Signals, not verdicts: they say where to look. The orphan-like group costs very little; the bigger prize is "
                   "assigning owners to the ownership-gap group (fill `data/owners_*.csv`).")
        cand = view[(view.kind == "Orphan-like") & ~view["account"].map(df[df.provider == provider].drop_duplicates("account").set_index("account")["status"]).isin(["Orphan", "Deleted", "Remove"])]
        pick = st.multiselect(f"Mark as Orphan in the owner file (status column)", list(cand["account"]), key=f"mark_orphan_{provider}",
                              help="Writes status = Orphan for the chosen rows (the file is backed up first; rows not in the file are added). Change it back any time in Data Health → Owners.")
        if pick and st.button(f"Mark {len(pick)} as Orphan", key=f"mark_orphan_btn_{provider}"):
            n = ST.set_status(provider, pick, "Orphan", "orphans page", "screening: " + ", ".join(sorted(set(cand[cand.account.isin(pick)].signal)))[:80])
            st.cache_data.clear()
            st.success(f"Marked {n} {noun.lower()}(s) as Orphan.")

    # ------------------------------------------------------------------ deep audit: run
    st.subheader("2. Deep audit — access and activity (your credentials)")
    if provider == "GCP":
        names_path = resolve(load_config()["audit"]["gcp_names"])
        cur = names_path.read_text() if names_path.exists() else ""
        with st.expander("Projects to audit (names or IDs, one per line)"):
            txt = st.text_area("List", cur, height=180, key="gcp_names_txt", label_visibility="collapsed")
            add = []
            if not scr.empty:
                add = st.multiselect("Add from screening (orphan-like / unowned)", list(scr[scr.kind != "OK"].account), key="gcp_add")
            if st.button("Save list"):
                names = [l.strip() for l in txt.splitlines() if l.strip()] + [a for a in add if a not in txt]
                names_path.write_text("\n".join(dict.fromkeys(names)) + "\n")
                st.success(f"Saved {len(set(names))} names")
        st.caption("Needs `gcloud auth application-default login`. Log reads are rate limited: about 15–20 minutes for ~30 projects.")
        extra = ()
    else:
        prof = st.text_input("AWS profile (blank = SSO browser login)", key="aws_audit_profile")
        st.caption("Run from the management account. Reads Organizations (status, OU, tags), IAM Identity Center (who has access) and the IAM "
                   "organizations access report (last activity). Anything your role cannot read is noted and skipped.")
        extra = ("--profile", prof) if prof else ()
    c1, c2 = st.columns([1, 3])
    if c1.button(f"Run {provider} deep audit", disabled=_running(provider), key=f"run_{provider}"):
        _launch(provider, extra)
        st.rerun()
    _progress(provider)

    # ------------------------------------------------------------------ deep audit: results
    runs = au.list_audits(provider)
    if not runs:
        st.info("No deep audit yet. Click the button above.")
        return
    pick = st.selectbox("Audit run", runs, format_func=lambda p: p.stem.replace("_", " ").upper(), key=f"run_pick_{provider}")
    import json
    data = json.loads(pick.read_text())
    rows = data["rows"]
    st.caption(f"As of {data['as_of']} · {len(rows)} {noun.lower()}s" + (f" · {data['note']}" if data.get("note") else ""))
    # Notes that apply to every row (e.g. "Organizations not readable") are shown once here, not on every detail card.
    hide = tuple(load_config().get("audit", {}).get("hide_notes", []))
    note_sets = [set(r.get("notes", [])) for r in rows]
    common = set.intersection(*note_sets) if len(rows) > 1 and note_sets else set()
    for n in sorted(common):
        if not n.startswith(hide):
            st.warning(n + ("  (applies to every row; statuses fall back to cost + owner file)" if "not readable" in n else ""))
    shown_notes = lambda r: [n for n in r.get("notes", []) if n not in common and not n.startswith(hide)]
    a = _status_blocks(rows)

    left, right = st.columns([2, 3])
    with left:
        g = a.groupby("status_label").agg(n=("name", "size"), avg3=("avg3", "sum")).reset_index()
        fig = px.bar(g, x="n", y="status_label", orientation="h", color="status_label", template=TEMPLATE,
                     color_discrete_map={au.STATUS_LABEL[k]: v for k, v in au.STATUS_COLOR.items()}, text="n",
                     labels={"n": f"{noun}s", "status_label": ""})
        fig.update_layout(showlegend=False, height=260, margin=dict(t=10, b=10))
        show(fig, width="stretch", key=f"stat_{provider}")
    with right:
        st.markdown("**Recommended next steps**")
        steps = []
        for r in sorted([r for r in rows if r["status"] == "spend"], key=lambda r: -r["avg3"])[:3]:
            top = r["services"][0]["name"] if r.get("services") else "unknown services"
            steps.append(f"**Find the owner of {r['name']}** — about {money(r['avg3'])}/mo ({money(r['cost12'])} in 12 months), mostly {top}.")
        idle = [r["name"] for r in rows if r["status"] == "idle"]
        if idle:
            steps.append(f"**Close/shut down {len(idle)} idle {noun.lower()}s** after a final check with the owner: " + ", ".join(idle[:12]) + ("…" if len(idle) > 12 else ""))
        gone = [r for r in rows if r["status"] == "gone"]
        if gone:
            steps.append(f"**Remove {len(gone)} no-access/gone entries from tracking** once deletion is confirmed.")
        steps.append("**Fix labels/tags and owners** so the next audit can name an owner without digging through logs.")
        for s in steps:
            st.markdown(f"- {s}")

    flt = st.multiselect("Status", [au.STATUS_LABEL[k] for k in au.STATUS_ORDER], default=[au.STATUS_LABEL[k] for k in ("spend", "idle", "gone", "review")], key=f"flt_{provider}")
    t = a[a.status_label.isin(flt)]
    tbl = t[["name", "id", "status_label", "state", "cost12", "avg3", "last_activity", "days_activity", "n_access", "action"]].copy()
    tbl["last_activity"] = tbl["last_activity"].astype(str).str[:10].replace({"None": "—", "nan": "—"})
    st.dataframe(tbl.assign(cost12=tbl.cost12.map(money), avg3=tbl.avg3.map(money)).rename(columns={
        "name": noun, "status_label": "status", "cost12": "last 12 mo", "avg3": "avg 3 mo", "days_activity": "days since activity", "n_access": "# with access"}),
        hide_index=True, width="stretch")
    st.download_button("Download audit CSV", a.drop(columns=["status_label"]).to_csv(index=False).encode(), f"{provider}_orphan_audit_{data['as_of']}.csv", "text/csv", key=f"dla_{provider}")
    gone_rows = [r["name"] for r in rows if r["status"] == "gone" or str(r.get("state", "")).upper() in ST.GONE_STATES]
    if gone_rows:
        st.info(f"The audit says {len(gone_rows)} {noun.lower()}(s) were removed or closed by the provider.")
        if st.button(f"Mark those {len(gone_rows)} as Deleted in the owner file", key=f"mark_deleted_{provider}",
                     help="Writes status = Deleted (confirmed by this audit). The file is backed up first; the change is logged in data/status_log.csv."):
            n = ST.set_status(provider, gone_rows, "Deleted", "orphans page", f"deep audit {data['as_of']}")
            st.cache_data.clear()
            st.success(f"Marked {n} {noun.lower()}(s) as Deleted.")

    # ------------------------------------------------------------------ detail
    st.markdown(f"**{noun} detail**")
    who = st.selectbox(f"Choose a {noun.lower()}", [r["name"] for r in rows], key=f"detail_{provider}")
    r = next(x for x in rows if x["name"] == who)
    st.markdown(f"### {r['name']}  ·  `{r['id']}`  ·  {au.STATUS_LABEL.get(r['status'], r['status'])}")
    st.info(r["action"])
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Last 12 mo", money(r["cost12"]))
    d2.metric("Avg last 3 mo", money(r["avg3"]))
    d3.metric("Last activity", str(r.get("last_activity") or "—")[:10], f"{r['days_activity']} days ago" if r.get("days_activity") is not None else None, delta_color="off")
    d4.metric("# with access", r.get("n_access") if r.get("n_access") is not None else "?")
    st.caption(f"Activity basis: {r.get('activity_basis', '—')}" + (f" · Created {r['created']}" if r.get("created") else "") + (f" · {r['parent']}" if r.get("parent") else ""))
    if r.get("months"):
        mf = pd.DataFrame({"month": r["month_labels"], "net": r["months"]})
        fig = px.bar(mf, x="month", y="net", template=TEMPLATE, labels={"net": "Net cost ($)", "month": ""}, color_discrete_sequence=[color])
        fig.update_layout(height=240, margin=dict(t=10, b=10))
        show(fig, width="stretch", key=f"mo_{provider}")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Top services**")
        if r.get("services"):
            sv = pd.DataFrame(r["services"])[["name", "net"]]
            st.dataframe(sv.assign(net=sv.net.map(money)), hide_index=True, width="stretch")
        st.markdown("**Owners**")
        st.write(", ".join(r.get("owners") or []) or "None found")
        if r.get("labels"):
            st.markdown("**Labels / tags**")
            st.write("; ".join(f"{k}={v}" for k, v in r["labels"].items()))
    with c2:
        st.markdown("**Who has access**")
        if r.get("members"):
            mm = pd.DataFrame([{"principal": x["m"], "roles / permission sets": ", ".join(x["roles"])} for x in r["members"]])
            st.dataframe(mm, hide_index=True, width="stretch")
        else:
            st.write("No named access found / not readable")
        sa = r.get("top_sa") or []
        if sa:
            st.markdown("**Top service accounts / services still active**" if provider == "GCP" else "**Services with recent authenticated use**")
            st.dataframe(pd.DataFrame(sa), hide_index=True, width="stretch")
    for n in shown_notes(r):
        st.warning(n)


tab_aws, tab_gcp = st.tabs(["AWS accounts", "GCP projects"])
with tab_aws:
    render("AWS", "Account")
with tab_gcp:
    render("GCP", "Project")
