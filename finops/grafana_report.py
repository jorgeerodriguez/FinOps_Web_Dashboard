"""Grafana Cloud spend as a self-contained HTML report (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

import pandas as pd

from . import grafana as gf
from . import platforms
from .config import ROOT
from .report import PAGE_W, Report, stamp
from .grafana import money


def build_report(data: dict, budget: float = 0.0) -> str:
    d = gf.summarize(data, budget)
    if d is None:
        raise ValueError("No Grafana data to report on.")
    prod = gf.product_table(d)
    W = PAGE_W - 40
    r = Report(f"Grafana Cloud — {d['last']:%B %Y}", f"Usage cost reported by Grafana (not invoices) · {d['months']} complete months through {d['last']:%B %Y} · independent of AWS and GCP")

    r.section()
    r.kpis(gf.kpi_items(d))
    r.bullets(gf.findings(d, prod))

    r.section("Monthly cost by product")
    r.chart(gf.fig_monthly(d), W, 330)
    r.caption("The grey outlined bar is the month in progress (a running estimate)." + (" The dashed line is the monthly budget." if budget else ""))

    r.section(f"{d['last']:%B %Y} by product", new_page=True)
    fc = gf.fig_commit(d)
    left = r.table_html(gf.product_display(prod.head(10), d["last"], d["prev"]))
    if fc is not None:
        r.columns([left, r.figure(fc, 330, 300)], [W - 340, 340])
    else:
        r.parts.append(left)
    fd = gf.fig_drivers(d)
    if fd is not None:
        r.parts.append("<h3>What drives the cost (volume)</h3>")
        r.chart(fd, W, 230)

    act = d["facts"].get("activity") or {}
    if act.get("active_users") or act.get("dashboards_total") is not None:
        r.section("Who uses Grafana (activity, not cost)")
        au = act.get("active_users") or {}
        bits = [f"**{au[k]:,}** active users in {k} days" for k in ("7", "30", "90") if k in au]
        if act.get("dashboards_total") is not None:
            bits.append(f"**{act.get('dashboards_active_90d', 0):,}** of **{act['dashboards_total']:,}** dashboards viewed in 90 days")
        r.bullets(bits)
        r.caption("Counts only; no user names or ids. Unviewed dashboards have no dollar cost, so no saving is claimed for them.")

    r.section("Notes", flow=True)
    rec = d["reconciliation"]
    notes = ["**Source:** the stack's usage datasource (read-only). Costs are Grafana-reported usage costs, not invoices; the month in progress is a running estimate.",
             "**Method:** the last value of each month's running overage is that month's cost; per-product values add up to Grafana's own total."]
    if len(rec):
        notes.append(f"**Reconciliation:** {int(rec['ok'].sum())} of {len(rec)} months match the prepaid commit drawdown within 2%.")
    notes.append("**No team attribution:** the usage metrics carry no team label.")
    r.bullets(notes)
    return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {platforms.last_refresh(gf.PLATFORM)} · FinOps Web Dashboard")


def save_report(html: str, last: pd.Timestamp) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"FinOps_Grafana_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
