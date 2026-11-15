"""Next Year Forecast as a self-contained HTML report (same builders as the page)."""
from __future__ import annotations
import getpass
from pathlib import Path

from . import history as hist
from . import nextyear_charts as ch
from . import nextyear_summary as nys
from . import store
from .config import ROOT
from .nextyear import NextYear
from .report import PAGE_W, Report, md as md_, stamp
from .ui import money

W = PAGE_W - 40


def build_report(ny: NextYear, budgets: dict, assumed: bool = False, lookback: dict | None = None, bu: dict | None = None) -> str:
    """bu: optional {scope: BottomUp} for AWS, GCP and AWS + GCP (the grouping, model and window chosen on the page); default = by account / project."""
    fy = ny.fy
    sm = nys.summarize(ny, budgets)
    r = Report(f"FY{fy} Cloud Budget Forecast",
               f"AWS + GCP · **Cloud Services** held to budget · Marketplace and Tax reported separately · built from the last 12 billed months "
               f"({ny.basis[0]:%b %Y} – {ny.last:%b %Y})")

    # ---- page 1: summary
    r.section()
    r.kpis(nys.kpi_items(ny, sm))
    r.bullets(nys.bullets(ny, sm))
    if not ny.adjusted:
        r.banner("error", nys.adjustments_lines(ny)[0])
    if assumed:
        r.banner("info", f"FY{fy} budget is not set yet: the FY{fy - 1} monthly budgets ({money(budgets['AWS'])} AWS, {money(budgets['GCP'])} GCP) are used as a reference.")
    ft = nys.fy_summary_df(ny, sm)
    fig = ch.fy_bridge(ny, budgets)
    r.columns([r.figure(fig, 440, 250),
               "<h3 style='margin-top:0'>FY summary (Cloud Services)</h3>" +
               r.table_money(ft, money, var_cols=("Var. (Base)",), strong_rows=("Total",)) +
               '<p class="cap">Variance = budget − forecast: green = under budget, red = over.</p>'],
              [450, W - 450])

    # ---- page 2: provider charts + growth drivers
    r.section("Cloud Services — actual, base forecast and range")
    r.columns([r.figure(ch.services_chart(ny, "AWS", budgets["AWS"]), W // 2 - 6, 290),
               r.figure(ch.services_chart(ny, "GCP", budgets["GCP"]), W // 2 - 6, 290)], [W // 2, W // 2])
    r.parts.append("<h3>What is driving the growth? — services by trend ($ added per month, per month)</h3>")
    r.columns([f"<b>AWS</b>" + r.table_html(nys.drivers_display(ny, "AWS", 5), index=True),
               f"<b>GCP</b>" + r.table_html(nys.drivers_display(ny, "GCP", 5), index=True)], [W // 2, W // 2])

    # ---- page 3: tables
    r.section("Budget tables", new_page=True)
    qt = nys.quarterly_df(ny, budgets)
    r.parts.append("<h3>By quarter — Services vs budget</h3>")
    r.parts.append(r.table_money(qt, lambda v: money(v) if abs(v) >= 0.5 else "$0", var_rows=[i for i in qt.index if "Variance" in i],
                                 strong_rows=[i for i in qt.index if i.startswith("Total — Base")]))
    r.caption("Variance = budget − forecast. Green = under budget, red = over.")
    mt = nys.monthly_df(ny, budgets, "Combined", "Base")
    r.section("By month — Services (base) + Marketplace", new_page=True)
    r.parts.append(r.table_money(mt, lambda v: money(v) if abs(v) >= 0.5 else "$0", strong_rows=(f"FY{fy}",)))
    r.caption("Marketplace is outside the Cloud Services budget; the month table is Services (base scenario) plus the scheduled Marketplace charges.")

    # ---- page 4: marketplace + combined
    r.section("Marketplace — contract renewal calendar", new_page=True)
    mq = nys.marketplace_quarter_df(ny)
    r.columns([r.figure(ch.marketplace_chart(ny), 590, 320),
               "<h3 style='margin-top:0'>By quarter</h3>" + r.table_money(mq, money, strong_rows=(f"FY{fy}",), index_name="Quarter")], [610, W - 610])
    tc = nys.top_contracts_display(ny, 8)
    if tc is not None:
        r.parts.append("<h3>Top contracts</h3>")
        r.table(tc)
        r.caption("Cadence is read from the last 12 billed months; annual contracts repeat at last year's price.")

    # ---- page 5: combined + reconciliation
    r.section("Combined view and reconciliation", new_page=True)
    r.columns([r.figure(ch.combined_chart(ny, budgets), 590, 400), r.figure(ch.reconciliation(ny, budgets), W - 610, 400)], [610, W - 610])
    r.caption("Left: Services + Marketplace by month (solid = billed, light = forecast). Right: the provider-level (top-down) forecast against the sum of the "
              "per-account / per-project (bottom-up) models.")

    # ---- bottom-up: both share charts on one page, (combined page for cross-cloud groupings), then a table page per provider
    if bu:
        ref = bu["AWS"]
        title = f"Where FY{fy} lands — by {ref.label.lower()} (bottom-up)"
        pick = (f"<b>Grouped by:</b> {ref.label} · <b>Model:</b> {ref.model} · <b>Training window:</b> "
                f"{'all billed months' if not ref.window else f'last {ref.window} months'}" + (" · <b>reconciled to the top-down total</b>" if ref.scale != 1.0 else ""))
    else:
        title, pick = f"Where FY{fy} lands — by account and by project (bottom-up)", ""
    r.section(title, new_page=True)
    r.caption(nys.BU_CAPTION if bu else nys.ACCOUNT_CAPTION)
    if pick:
        r.parts.append(f"<p style='font-size:12px;margin:2px 0 6px'>{pick}</p>")
    scopes = [("AWS", "AWS accounts" if not bu or bu["AWS"].dim == "account" else "AWS"),
              ("GCP", "GCP projects" if not bu or bu["GCP"].dim == "account" else "GCP")]
    tab = {p: (bu[p].table if bu else ny.tables[p]) for p, _ in scopes}
    cells = []
    for p, label in scopes:
        text = nys.bu_text(bu[p]) if bu else nys.account_text(ny, sm, p)
        cells.append(f"<h3 style='margin-top:6px'>{label}</h3><p style='font-size:12px;margin:0 0 4px'>{md_(text)}</p>"
                     + r.figure(ch.accounts_share(tab[p], ny, p, 22), W // 2 - 6, 30 * min(len(tab[p]), 14) + 60))
    r.columns(cells, [W // 2, W // 2])
    if bu and ref.dim != "account" and bu.get("AWS + GCP") is not None and len(bu["AWS + GCP"].table):
        both = bu["AWS + GCP"]
        r.section(f"AWS + GCP together — by {ref.label.lower()}", new_page=True, flow=True)
        r.parts.append(f"<p style='font-size:12px;margin:0 0 4px'>{md_(nys.bu_text(both))}</p>")
        r.parts.append(r.figure(ch.accounts_share(both.table, ny, "AWS + GCP", 30), W, 24 * min(len(both.table), 16) + 60))
        r.table(nys.bu_display(both), index=True)
    for p, label in scopes:
        r.section(f"{label} — FY{fy} forecast by line", new_page=True, flow=True)
        r.table(nys.bu_display(bu[p]) if bu else nys.account_display(ny, p), index=True)
        if bu:
            notes = sorted({lf.note for lf in bu[p].lines if lf.note})
            if notes:
                r.caption("† " + " · ".join(notes))
            v = nys.model_verdict(bu[p])
            if v:
                r.caption(v.replace("**", ""))

    # ---- look-back
    if lookback is not None:
        r.section("How did this method do last year?", new_page=True)
        for t in hist.lookback_text(lookback)[:1]:
            r.text(t)
        r.chart(hist.fig_lookback(lookback), height=300)
        r.table(hist.lookback_table(lookback))
        r.caption("The same method re-run with only the data available 12 months before the latest billed month (no reviewed adjustments), against what was billed. "
                  "It shows how far to trust the Low–High ranges above.")

    # ---- anomalies + method
    an = nys.anomalies_display(ny)
    r.section("Data anomalies — review status, and method", new_page=True, flow=True)
    if an is not None:
        r.caption(nys.ANOMALY_CAPTION)
        r.table(an.drop(columns=["Months near $0"]).head(3))
        if len(an) > 3:
            r.caption(f"{len(an) - 3} more in the dashboard (Next Year Forecast → Data anomalies — review status); "
                      "pending items are listed first.")
    r.parts.append("<h3>Method & caveats</h3>")
    for para in [x.strip() for x in nys.METHOD_MD.split("\n\n") if x.strip()]:
        r.small(para.replace("\n", " "))

    log = store.read_log()
    last_refresh = log["when"].max() if len(log) else "unknown"
    return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {last_refresh} · source: AWS Cost Explorer and GCP BigQuery billing export · FinOps Web Dashboard")


def save_report(html: str, ny: NextYear) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"FinOps_Next_Year_Forecast_FY{ny.fy}.html"
    p.write_text(html, encoding="utf-8")
    return p
