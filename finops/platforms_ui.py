"""One-line summaries of the non-cloud platforms for the Executive Summary. Never added to the AWS / GCP totals."""
from __future__ import annotations
import pandas as pd
import streamlit as st

from . import equinix as eq
from .ui import money


def _equinix() -> dict | None:
    d = eq.summarize(*eq.read())
    if d is None:
        return None
    mom = eq.pct(d["now"], d["before"])
    return dict(name="Equinix", month=d["last"], value=d["now"], run_rate=d["run_rate"], last12=d["last12"],
                delta="" if pd.isna(mom) else "no change vs prior month" if abs(d["now"] - d["before"]) < 0.5 else f"{mom:+.1%} vs prior month", page="views/equinix.py")


def _grafana() -> dict | None:
    from . import grafana as gf
    d = gf.summarize(gf.read(), 0.0)
    if d is None:
        return None
    mom = gf.pct(d["now"], d["before"])
    cs = d["commit"]
    return dict(name="Grafana Cloud", month=d["last"], value=d["now"], fmt=gf.money, run_rate=d["total"].tail(3).mean(), last12=d["last12"],
                delta="" if pd.isna(mom) else "no change vs prior month" if abs(d["now"] - d["before"]) < 0.5 else f"{mom:+.1%} vs prior month", page="views/grafana.py",
                note=(f" Commit remaining {money(cs['balance'])}." if cs else ""))


def summaries() -> list[dict]:
    out = []
    for fn in (_equinix, _grafana):
        try:
            s = fn()
        except Exception:                      # a broken platform file must never take the Executive page down
            s = None
        if s:
            out.append(s)
    return out


def executive_strip() -> None:
    items = summaries()
    if not items:
        return
    st.subheader("Other platforms (not in the cloud totals or budget)")
    for col, s in zip(st.columns(max(len(items), 2)), items):
        col.metric(f"{s['name']} — {s['month']:%b %Y}", s.get("fmt", money)(s["value"]), s["delta"] or None, delta_color="inverse",
                   help=f"Run-rate {money(s['run_rate'])} a month · {money(s['last12'])} over the last 12 months.{s.get('note', '')} Independent of AWS and GCP.")
        try:
            col.page_link(s["page"], label=f"Open {s['name']}")
        except Exception:                      # not under st.navigation (tests, embedded runs): the metric is enough
            pass
