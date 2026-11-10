"""Two-Year History as a self-contained HTML report (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

import pandas as pd

from . import history as H
from . import store
from .config import ROOT
from .report import PAGE_W, Report, stamp
from .ui import force_view, get_data

CLOUD_VIEW = "Cloud Services (budget basis)"
W = PAGE_W - 40


def add_overview(r: Report, h: H.History, compact: bool = False) -> None:
    """KPIs + findings + the two main trend charts. `compact` is used when embedded in another report."""
    r.kpis(H.kpi_items(h))
    r.bullets(H.narrative(h)[: 3 if compact else 6])
    fig = H.fig_trend(h)
    fig.update_layout(height=240 if compact else 290, title=dict(text="Every billed month — spend by provider, 12-month average and budget", font=dict(size=14)),
                      margin=dict(t=40))
    r.chart(fig, height=240 if compact else 290)


def add_story(r: Report, title: str, lines: list[str]) -> None:
    r.parts.append(f"<h3>{title}</h3>")
    bullets = []
    for ln in lines:
        if ln.startswith("- "):
            bullets.append(ln[2:])
            continue
        if bullets:
            r.bullets(bullets); bullets = []
        if ln.startswith("### "):
            r.parts.append(f"<h3 style='font-size:13px;margin:8px 0 2px'>{ln[4:]}</h3>")
        else:
            r.small(ln)
    if bullets:
        r.bullets(bullets)


def build_report() -> str:
    with force_view(CLOUD_VIEW):
        allc = get_data(apply_view=False)
        h = H.compute(allc[allc["category"] == "Cloud Services"])
        r = Report(f"FinOps Two-Year History — {h.last:%B %Y}",
                   f"AWS + GCP · **Cloud Services (budget basis)** · {h.first:%b %Y} – {h.last:%b %Y} ({len(h.m)} billed months) · "
                   "latest 12 months compared with the 12 before")

        # page 1: headline numbers, findings, the whole timeline
        r.section()
        add_overview(r, h)

        # page 2: year over year + run-rate + bridge
        r.section("Same month, year over year")
        fy = H.fig_yoy(h)
        r.chart(fy, height=260)
        
        fr, fb = H.fig_rolling(h), H.fig_bridge(h)
        fr.update_layout(title=dict(text="Annual run-rate (trailing 12 months)", font=dict(size=14)), margin=dict(t=40))
        cells, widths = [r.figure(fr, 360, 330)], [370]
        if fb is not None:
            fb.update_layout(title=dict(text="What explains the change (by service)", font=dict(size=14)), margin=dict(t=40, b=10))
            cells.append(r.figure(fb, W - 380, 330))
            widths.append(W - 370)
        r.columns(cells, widths)
        r.caption("YoY chart: one line per year, newest is heavy. Right: prior 12 months → latest 12 months by service; red = up, green = down.")

        # page 3: movers
        r.section("Biggest changes — latest 12 months vs the 12 before", new_page=True, flow=True)
        r.parts.append("<h3>By service</h3>")
        r.table(H.movers_display(H.movers(h, "service", 8)))
        r.parts.append("<h3>By account / project</h3>")
        r.table(H.movers_display(H.movers(h, "account", 8)))
        r.caption("“New” = under $1K in the prior 12 months. Multiples (e.g. 4×) mean the latest 12 months cost that many times the prior 12.")
        # page 4: separate + look-back
        r.section("Reported separately (not in budget)", new_page=True)
        r.columns([r.figure(H.fig_marketplace(allc, h), 520, 240), r.table_html(H.separate_summary(allc, h))], [540, W - 540])
        r.caption("Marketplace and Tax are never mixed into the Cloud Services budget view.")
        sd = H.spikes_display(h)
        if not sd.empty:
            r.parts.append("<h3>One-off months</h3>")
            r.table(sd.head(5))
            r.caption("Months ≥35% and ≥$25K away from the median of the 3 months either side. Confirm with the owner before treating them as trend.")



        lb = H.lookback(allc)
        if lb is not None:
            r.section("Would our forecast method have called it?")
            for t in H.lookback_text(lb)[:1]:
                r.text(t)
            r.chart(H.fig_lookback(lb), height=260)
            r.table(H.lookback_table(lb))
            r.caption("The Next Year method re-run with only the data available 12 months ago (no reviewed adjustments), against what was billed. "
                      "It shows how far to trust the Low–High ranges.")
        # last pages: the executive summary in words, one section per lens
        for i, (title, lines) in enumerate(H.story_sections(h)):
            r.section("Executive summary — what moved, in words" if i == 0 else None, new_page=True, flow=True)
            if i == 0:
                r.caption("Latest 12 months vs the 12 before. A one-off or a shutdown is not counted as a saving.")
            add_story(r, title, lines)
        log = store.read_log()
        r.caption("Windows: latest 12 billed months vs the 12 before, and the same calendar months year over year, so a part-year is never compared with a full year. "
                  f"One-off months are flagged, never removed from totals. Billed data from {h.first:%B %Y}; the in-progress month is excluded (see Month to Date).")
        last_refresh = log["when"].max() if len(log) else "unknown"
        return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {last_refresh} · source: AWS Cost Explorer and GCP BigQuery billing export · FinOps Web Dashboard")


def save_report(html: str, last: pd.Timestamp) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"FinOps_Two_Year_History_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
