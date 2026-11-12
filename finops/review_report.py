"""A printable review pack for ONE owner, project code or cost center: what it costs, what changed, savings, what came back, notes, open gaps."""
from __future__ import annotations
import getpass
import re
from pathlib import Path

import pandas as pd

from . import dims as D
from . import history as H
from . import savings as S
from . import store
from .config import ROOT
from .report import PAGE_W, Report, stamp
from .ui import force_view, get_data, money

CLOUD_VIEW = "Cloud Services (budget basis)"
W = PAGE_W - 40
GAP_FIELDS = (("environment", "environment"), ("project_code", "project code"), ("cost_center", "cost center"))


def build_review(by: str, who: str, materiality: float = 5_000.0) -> str:
    with force_view(CLOUD_VIEW):
        allc = get_data(apply_view=False)
        df = allc[allc["category"] == "Cloud Services"]
        h = H.compute(df)
        acct = S.annotate(S.scan(S.adjusted_series(df, "account"), materiality, S.owners_map(df, "account")), df, "account")
        d = S.owner_detail(df, h, who, acct, by=by)
        sub = df[df[by] == who]
        label = S.BY_LABEL[by]
        r = Report(f"FinOps review — {who}", f"{label} review · AWS + GCP · **Cloud Services (budget basis)** · latest 12 months ({h.cur[0]:%b %Y} – {h.last:%b %Y}) vs the 12 before")
        ch = d["latest"] - d["prior"]
        red, giv = d["ledger"][d["ledger"].cls == "Reduced"], d["erosion"]
        r.section()
        r.kpis([dict(label="Latest 12 months", value=money(d["latest"]), delta=(("+" if ch > 0 else "") + money(ch)) + " vs prior", inverse=True),
                dict(label="Prior 12 months", value=money(d["prior"]), delta="", neutral=True),
                dict(label="Savings run-rate", value=money(red["lower"].sum()) + "/mo", delta=f"{len(red)} lines", neutral=True),
                dict(label="Realized to date", value=money(red.realized.sum()), delta="", neutral=True),
                dict(label="Given back", value=money(giv.regrown.sum()) + "/mo", delta=f"{len(giv)} lines", neutral=True)])
        if d["notes"]:
            r.parts.append("<h3>What it is (finance notes)</h3>")
            r.bullets(d["notes"][:2])
        d["fig"].update_layout(height=190, margin=dict(t=4, b=4), title=None, legend=dict(font=dict(size=8)))
        r.chart(d["fig"], height=190)
        a = d["accounts"]
        at = pd.DataFrame({"Provider": a.provider, "Account / project": a.name.str.slice(0, 40), "Prior 12 mo": a.prior.map(money), "Latest 12 mo": a.latest.map(money),
                           "Change": a.change.map(lambda v: ("+" if v > 0 else "") + money(v)),
                           "%": a.pct.map(lambda v: "new" if pd.isna(v) else D.chg(v))})
        r.parts.append("<h3>Accounts and projects</h3>")
        r.table(at.head(5))
        r.caption(f"Top 5 of {len(at)} accounts / projects by spend." if len(at) > 5 else "")
        r.section(f"What changed — {who}", new_page=True)
        rising = a[a.change > 0].sort_values("change", ascending=False).head(5)
        if len(rising):
            r.parts.append("<h3>Biggest increases to ask about</h3>")
            r.bullets([f"{x.provider} · {x.name[:40]}: +{money(x.change)}" + (" (new)" if x.prior < 1000 else "") for x in rising.head(3).itertuples()])
        if len(d["ledger"]):
            r.parts.append("<h3>Step-downs credited here</h3>")
            r.table(S.ledger_display(d["ledger"]).drop(columns=["Owner"] if "Owner" in S.ledger_display(d["ledger"]) else []).head(3))
        if len(giv):
            r.parts.append("<h3>Savings that came back</h3>")
            r.table(S.erosion_display(giv).drop(columns=["Owner"]).head(3))
        r.parts.append("<h3>Open gaps</h3>")
        gaps = []
        for col, nice in GAP_FIELDS:
            cv = D.coverage(sub, col)
            if cv["missing"] >= 1 and len(cv["worst"]):
                gaps.append(f"**{nice}** missing on {money(cv['missing'])} of the latest 12 months: " + ", ".join(cv["worst"]["Account / project"].head(3)))
        r.bullets(gaps or ["Nothing missing: every account / project here has an environment, project code and cost center."])
        r.caption("Savings are measured against each line's own earlier peak. Cost data shows what changed, not why: the owner must confirm each item.")
        log = store.read_log()
        last_refresh = log["when"].max() if len(log) else "unknown"
        return r.render(f"Generated {stamp()} by {getpass.getuser()} · data last refreshed {last_refresh} · source: AWS Cost Explorer and GCP BigQuery billing export · FinOps Web Dashboard")


def save_review(html: str, by: str, who: str) -> Path:
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    a = store.read()
    last = pd.Timestamp(a[~a["is_partial"].astype(bool)]["date"].max())
    slug = re.sub(r"[^A-Za-z0-9]+", "_", who).strip("_")[:40] or "review"
    p = d / f"FinOps_Review_{by}_{slug}_{last:%Y-%m}.html"
    p.write_text(html, encoding="utf-8")
    return p
