import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from finops import store
from finops.ui import get_data, money

ROOT = Path(__file__).resolve().parent.parent
st.title("Data Health & Refresh")

# ---------------------------------------------------------------- refresh (uses YOUR credentials)
st.subheader("Refresh data")
st.caption("Runs on this machine with **your own** credentials — AWS SSO sign-in (browser) and GCP "
           "`gcloud auth application-default login`. Nothing is stored by the dashboard.")
c1, c2, c3 = st.columns([1, 1, 2])
profile = c3.text_input("AWS profile (optional — blank = SSO browser login)", value="")
choice = None
if c1.button("Refresh AWS"):
    choice = "aws"
if c2.button("Refresh GCP"):
    choice = "gcp"
if choice:
    cmd = [sys.executable, "-u", "-m", "ingest.run", "--provider", choice] + (["--profile", profile] if profile and choice == "aws" else [])
    with st.status(f"Refreshing {choice.upper()}…", expanded=True) as status:
        box = st.empty()
        lines = []
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            lines.append(line.rstrip())
            box.code("\n".join(lines[-15:]))
        ok = proc.wait() == 0
        status.update(label=f"{choice.upper()} refresh " + ("finished" if ok else "FAILED"), state="complete" if ok else "error")
    if ok:
        st.cache_data.clear()
        st.success("Data updated. The tables below show the new data.")

# ---------------------------------------------------------------- freshness
log = store.read_log()
st.subheader("Refresh history")
st.dataframe(log.sort_values("when", ascending=False) if len(log) else log, hide_index=True, width="stretch")

df = get_data(apply_view=False, include_partial=True)
if df.empty:
    st.info("No data yet. Click **Refresh AWS** / **Refresh GCP** above.")
    st.stop()

st.caption("Tables below show ALL categories regardless of the sidebar selection.")
g = df.groupby("provider").agg(first=("date", "min"), last=("date", "max"), rows=("cost_net", "size"),
                               total=("cost_net", "sum"), months=("date", "nunique"))
st.dataframe(g.assign(total=g.total.map(money)), width="stretch")
st.subheader("Monthly totals (check gaps / odd values)")
p = df.pivot_table(index="date", columns=["provider", "category"], values="cost_net", aggfunc="sum").sort_index(ascending=False)
st.dataframe(p.round(0), width="stretch")
part = df[df.is_partial]
if len(part):
    st.warning(f"Contains in-progress month(s) (as-now): {', '.join(sorted({f'{d:%b %Y}' for d in part.date}))} — "
               "excluded from billed views until 'as-now' mode is wired in.")
