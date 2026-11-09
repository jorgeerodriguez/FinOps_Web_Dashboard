"""Executive Summary as a self-contained HTML report (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

import pandas as pd

from . import exec_summary as es
from . import store
from .config import ROOT
from .exec_charts import entity_block
from .report import PAGE_W, Report, stamp
from .ui import force_view, get_data, money

CLOUD_VIEW = "Cloud Services (budget basis)"


def build_report(topn: int = 8, other_by: str = "Single band") -> str:
    with force_view(CLOUD_VIEW):
        allc = get_data(apply_view=False)
        df = allc[allc["category"] == "Cloud Services"]
        k = es.compute(df)
        r = Report(f"FinOps Executive Summary — {k.last:%B %Y}",
                   f"AWS + GCP · **Cloud Services (budget basis)** · billed through {k.last:%B %Y} · fiscal year Jan–Dec")

        W = PAGE_W - 40
        # ---- page 1: KPIs, verdict, the two budget charts side by side
        r.section()
        r.kpis(es.kpi_items(k))
        fig_cum, verdict, cap_cum = es.fig_cumulative(k)
        if verdict:
            r.banner(verdict[0], verdict[1])
        r.caption(cap_cum)
        fig_b, cap_b = es.fig_vs_budget(k)
        fig_b.update_layout(title=dict(text=f"{k.yr} by month vs budget", font=dict(size=14)), margin=dict(t=40))
        fig_cum.update_layout(title=dict(text=f"Cumulative spend vs budget — {k.yr}", font=dict(size=14)), margin=dict(t=40))
        r.columns([r.figure(fig_b, W // 2 - 6, 340), r.figure(fig_cum, W // 2 - 6, 340)], [W // 2, W // 2])
        r.caption(cap_b)

        # ---- page 2: provider trend + top services side by side, then the separately-reported table and notes
        r.section()
        fig_p = es.fig_provider_monthly(k)
        fig_p.update_layout(title=dict(text="Monthly spend by provider", font=dict(size=14)), margin=dict(t=40))
        ts = es.top_services(k, 8).rename(columns={"cost_net": "net cost"})
        r.columns([r.figure(fig_p, 540, 330), "<h3 style='margin-top:0'>Top services — latest month</h3>" + r.table_html(ts)], [560, W - 560])
        r.parts.append("<h3>Reported separately (not in budget)</h3>")
        r.table(es.separate_table(allc, k))
        r.caption("Marketplace (third-party contracts billed through the clouds) and Tax are never mixed into the Cloud Services budget view.")

        # ---- notes (kept on page 2 with the other reference tables)
        log = store.read_log()
        last_refresh = log["when"].max() if len(log) else "unknown"
        r.parts.append("<h3>Notes</h3>")
        r.bullets([
            f"**Budget:** {money(k.budget / 12)}/month for AWS + GCP Cloud Services ({money(k.budget)} per year); Marketplace and Tax are outside it.",
            "**Forecast:** each provider is modelled separately (same month last year × recent growth, or a flat run-rate when history is short), "
            "then summed. The range shown is the backtest error where it can be measured, otherwise ±15%.",
            "**History:** the dashboard holds about 14 months of billing data, so year-over-year comparisons cover only the months that overlap.",
        ])

        # ---- pages 3-6: one block per provider / level
        for provider, level, noun in (("AWS", "service", "Service"), ("AWS", "account", "Account"),
                                      ("GCP", "service", "Service"), ("GCP", "account", "Project")):
            blk = entity_block(allc[allc["category"] == "Cloud Services"], provider, level, noun, topn, other_by, cloud=True)
            if blk is None:
                continue
            blk["area"].update_layout(height=440, legend=dict(font=dict(size=9)))
            if blk["dumbbell"] is not None:
                blk["dumbbell"].update_layout(height=440)
            r.section(f"{provider} {noun}s — where the money goes")
            r.text(blk["msg"])
            if blk["note"]:
                r.banner("info", blk["note"])
            if blk["dumbbell"] is not None:
                r.charts_row([blk["area"], blk["dumbbell"]], [int((PAGE_W - 60) * 0.57), int((PAGE_W - 60) * 0.43)])
            else:
                r.chart(blk["area"], height=410)
                r.caption(f"No overlapping billed months with {blk['yr'] - 1} yet.")
            if blk["other_caption"]:
                r.caption(blk["other_caption"])

        footer = (f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {last_refresh} · source: AWS Cost Explorer and GCP BigQuery billing export "
                  "· FinOps Web Dashboard")
        return r.render(footer)


def save_report(html: str, last: pd.Timestamp) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"FinOps_Executive_Summary_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
