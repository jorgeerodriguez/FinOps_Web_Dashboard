"""Projects & Environments as a self-contained HTML report: AWS + GCP together, then one block per cloud (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

import pandas as pd

from . import dims as D
from . import store
from .config import ROOT
from .owners import MISSING
from .report import PAGE_W, Report, stamp
from .ui import force_view, get_data

CLOUD_VIEW = "Cloud Services (budget basis)"
W = PAGE_W - 40


def add_projects(r: Report, df: pd.DataFrame, title: str, shared: list[str], compact: bool = False, third: str | None = None, new_page: bool = False) -> None:
    """One page-sized block: findings, project-code trend and bars, the table, then environments."""
    view = df[~D.is_shared(df["project_code"], shared)] if shared else df
    r.section(title, new_page=new_page)
    r.kpis(D.kpi_items(df, shared))
    r.bullets(D.narrative(df, shared)[: 4 if compact else 6])
    a = D.fig_stack(view, "project_code", 8, title="Monthly cost by project code")
    b = D.fig_change(D.by_dim(view, "project_code", shared), "project_code", 8, title="Latest 12 months (change vs prior 12)")
    a.update_layout(title=dict(font=dict(size=14)), margin=dict(t=40, b=10), legend=dict(font=dict(size=9)))
    b.update_layout(title=dict(font=dict(size=14)), margin=dict(t=40, r=120))
    r.columns([r.figure(a, int(W * 0.57), 330), r.figure(b, int(W * 0.43) - 10, 330)], [int(W * 0.57) + 10, int(W * 0.43)])
    if shared:
        r.caption("Shared / enterprise buckets (★ in the table) are left out of the charts: one support contract or commitment fee can be half the bill.")
    if compact:
        return
    r.section(f"{title} — detail", new_page=True)
    t = D.by_dim(df, "project_code", shared)
    r.table(D.display(t.head(9), "project_code"))
    e1, e2 = D.fig_env(df, 24), D.fig_nonprod_share(df, 24)
    for f in (e1, e2):
        f.update_layout(title=dict(font=dict(size=13)), margin=dict(t=36, b=10), legend=dict(font=dict(size=9), y=-0.3))
    r.columns([r.figure(e1, int(W * 0.55), 230), r.figure(e2, int(W * 0.45) - 10, 230)], [int(W * 0.55) + 10, int(W * 0.45)])
    r.section(f"{title} — prod vs non-prod" + (f" and {D.LABEL[third].lower()}" if third and (df[third] != MISSING[third]).any() else ""))
    r.table(D.env_display(D.env_by_code(df).head(8)))
    r.caption("Non-prod % = non-prod / (prod + non-prod). Shared and unspecified environments are not counted in it.")
    if third and (df[third] != MISSING[third]).any():
        r.parts.append(f"<h3>By {D.LABEL[third].lower()}</h3>")
        r.table(D.display(D.by_dim(df, third), third).head(6))


def build_report() -> str:
    with force_view(CLOUD_VIEW):
        allc = get_data(apply_view=False)
        df = allc[allc["category"] == "Cloud Services"]
        cur, _, last = D.windows(df)
        shared = D.shared_codes()
        r = Report(f"FinOps Projects & Environments — {last:%B %Y}",
                   f"AWS + GCP · **Cloud Services (budget basis)** · latest 12 months ({(last - pd.DateOffset(months=11)):%b %Y} – {last:%b %Y}) vs the 12 before · "
                   "grouped by the project codes, environments and cost centers in the owner files")
        add_projects(r, df, "AWS and GCP together", shared, compact=True)
        r.section("Projects that run on both clouds, and Finance readiness")
        cc = D.cross_cloud(df)
        if len(cc):
            r.columns([r.figure(D.fig_cross(cc), int(W * 0.5), 280), r.table_html(D.cross_display(cc.head(8)))], [int(W * 0.5) + 10, int(W * 0.5)])
        else:
            r.text("No project code bills on both clouds above $5K a year each.")
        r.parts.append("<h3>Finance readiness — spend that carries each code</h3>")
        r.table(D.readiness_display(df))
        r.caption("Coverage is the share of the latest 12 months' spend whose account / project has a value in the owner file. "
                  "Blank and placeholder values count as not filled in.")
        for prov, third in (("AWS", "service_name"), ("GCP", "cost_center")):
            d = df[df["provider"] == prov]
            if not d.empty:
                add_projects(r, d, f"{prov}", shared, third=third)
        r.section("Where the money goes", new_page=True)
        tm = D.fig_treemap(df, shared)
        tm.update_layout(title=dict(font=dict(size=14)), height=470)
        r.chart(tm, height=470)
        r.caption("Size = latest 12 months, project code → cloud → account / project. Shared buckets left out.")
        log = store.read_log()
        last_refresh = log["when"].max() if len(log) else "unknown"
        return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {last_refresh} · source: AWS Cost Explorer and GCP BigQuery billing export, "
                        "owner files maintained in Data Health · FinOps Web Dashboard")


def save_report(html: str) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    a = store.read()
    last = pd.Timestamp(a[~a["is_partial"].astype(bool)]["date"].max())
    p = d / f"FinOps_Projects_Environments_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
