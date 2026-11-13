"""Equinix spend as a self-contained HTML report (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

import pandas as pd

from . import equinix as eq
from . import platforms
from .config import ROOT
from .report import PAGE_W, Report, stamp


def build_report(lines: pd.DataFrame, inv: pd.DataFrame) -> str:
    d = eq.summarize(lines, inv)
    if d is None:
        raise ValueError("No Equinix data to report on.")
    items = eq.latest_items(lines, d["last"])
    chg = eq.changes(lines, d["last"], d["prev"])
    W = PAGE_W - 40
    r = Report(f"Equinix — {d['last']:%B %Y}", f"Fabric and Network Edge · charges before tax, credits shown separately · {d['months']} months of invoices through {d['last']:%B %Y} · independent of AWS and GCP")

    r.section()
    r.kpis(eq.kpi_items(d))
    r.bullets(eq.findings(d, items, chg))

    r.section("Monthly charges by category")
    r.chart(eq.fig_monthly(d), W, 330)

    r.section(f"{d['last']:%B %Y} by item")
    top = items.head(10)
    r.columns([r.figure(eq.fig_items(top, 10), 470, 300), r.table_html(eq.items_display(top))], [480, W - 480])
    r.caption(f"Top {len(top)} of {len(items)} lines billed in {d['last']:%B %Y}.")

    if len(chg):
        r.section(f"What changed since {d['prev']:%B %Y}")
        r.table(eq.changes_display(chg.head(12)))
    fc = eq.fig_credits(d)
    if fc is not None:
        r.section("Credits")
        r.chart(fc, W, 200)
        r.caption("One-time refunds or adjustments. They are never counted as lower charges, run-rate reductions or savings.")

    r.section("Notes", flow=True)
    r.bullets([
        "**Source:** Equinix Billing API, read-only. Equinix keeps about 12 months of invoices, so each dashboard refresh adds to the history saved locally.",
        "**Charges** are recurring + one-time amounts on invoice lines, before tax. **Credits** are negative lines or credit memos and are reported on their own.",
        "**Run-rate** is the recurring total on the latest invoice.",
        "Utilization, right-sizing and BGP reliability come from the standalone Equinix report.",
    ])
    return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {platforms.last_refresh(eq.PLATFORM)} · FinOps Web Dashboard")


def save_report(html: str, last: pd.Timestamp) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"FinOps_Equinix_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
