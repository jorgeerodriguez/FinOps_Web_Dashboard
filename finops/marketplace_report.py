"""Marketplace renewal calendar as a self-contained HTML report (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

import pandas as pd

from . import marketplace_view as mv
from . import store
from .config import ROOT
from .report import PAGE_W, Report, stamp
from .ui import force_view, get_data, money

PRINT_ROWS = 14        # calendar rows that fit under the header and KPIs on one landscape page


def build_report(group: str = "Vendor") -> str:
    with force_view("Cloud Services (budget basis)"):
        allc = get_data(apply_view=False)
        mkt = allc[allc.category == "Marketplace"]
        d = mv.assemble(mkt, group, PRINT_ROWS)
        yr, W = d["yr"], PAGE_W - 40
        r = Report(f"Marketplace & Third-Party Contracts — {d['last']:%B %Y}",
                   f"AWS + GCP Marketplace · renewal calendar built from each contract's last 12 billed months · billed through {d['last']:%B %Y}")

        # page 1: KPIs + calendar
        r.section()
        r.kpis(d["kpis"])
        d["cal"].update_layout(height=22 * PRINT_ROWS + 100)
        r.chart(d["cal"])
        r.caption(d["cal_caption"] + " Marketplace is never part of the Cloud Services budget.")

        # page 2: monthly cash view + quarterly table + renewal watch
        r.section("Monthly cash view")
        d["bars"].update_layout(margin=dict(t=20))
        q = d["quarter"].to_frame("Scheduled")
        q.loc[f"FY{yr + 1}"] = q["Scheduled"].sum()
        r.columns([r.figure(d["bars"], 610, 330),
                   f"<h3 style='margin-top:0'>{yr + 1} by quarter</h3>" + r.table_money(q, money, strong_rows=(f"FY{yr + 1}",), index_name="Quarter")],
                  [630, W - 630])
        if len(d["risk"]):
            r.parts.append("<h3>Renewal watch — billed earlier, not in the last 12 months</h3>")
            r.table(mv.risk_display(d["risk"]).drop(columns=[c for c in ("id",) if c in d["risk"]], errors="ignore"))
            r.caption("Ended, or renewing outside the window. Confirm with the owner before budgeting.")

        # page 3+: contracts
        r.section(f"Contracts — {yr + 1} forecast", flow=True, new_page=True)
        cd = mv.contracts_display(d["contracts"], yr)
        cd = cd[cd["cadence"] != "Not billed in last 12 mo"].head(30)
        keep = ["provider", "vendor", "cadence", "per charge", "billed last 12 mo", f"{yr} (billed + sched.)", f"{yr + 1} forecast",
                f"billing months {yr + 1}", "next_charge"]
        r.table(cd[keep].rename(columns={"next_charge": "next charge"}))
        r.caption("Top contracts by next-year forecast. Cadence is read from the last 12 billed months; annual contracts repeat at last year's price.")

        r.section("Notes")
        log = store.read_log()
        r.bullets([
            "**Method:** monthly = average of the last 3 months; regular gap (quarterly, semiannual …) = last amount at that gap; "
            "a single charge = annual renewal in the same month next year. Charges under 10% of a contract's largest are ignored when reading the cadence.",
            "**History:** about 14 months of billing data, so an annual contract's price change at renewal is not anticipated.",
            "**Not a trend forecast:** Marketplace is planned as a calendar of commitments; confirm renewals with owners before budgeting.",
        ])
        last_refresh = log["when"].max() if len(log) else "unknown"
        return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {last_refresh} · FinOps Web Dashboard")


def save_report(html: str, last: pd.Timestamp) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"FinOps_Marketplace_Calendar_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
