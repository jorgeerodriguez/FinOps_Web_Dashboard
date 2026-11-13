"""Equinix spend: normalise the Billing API payloads, then the analysis behind the page and the report. No network calls here.

Equinix is an independent platform: it is never added to the AWS / GCP totals, budgets or savings. Credits are one-time refunds or
adjustments, so they are reported on their own and never counted as lower charges (same rule as the standalone Equinix report)."""
from __future__ import annotations
import re

import pandas as pd
import plotly.graph_objects as go

from . import platforms

PLATFORM = "Equinix"
LINES, INVOICES = "equinix_lines", "equinix_invoices"
LINE_COLS = ["month", "invoice_id", "kind", "category", "item", "product", "uuid", "activity", "amount", "tax", "currency"]
INV_COLS = ["month", "invoice_id", "type", "recurring", "one_time", "total", "currency"]

CATEGORIES = ["Google Cloud", "AWS", "Network Edge", "Cloud Router", "Internet Access", "Other"]
COLORS = {"Google Cloud": "#2563EB", "AWS": "#E07B00", "Network Edge": "#0F766E", "Cloud Router": "#7C3AED",
          "Internet Access": "#BE185D", "Other": "#94A3B8"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# ------------------------------------------------------------------------------------------- normalise (raw API -> tables)
def line_uuid(line: dict) -> str | None:
    """The asset an invoice line is for. Fabric connection lines carry the Cloud Router's UUID in additionalInfo but their own in the
    product description, so prefer the description."""
    m = UUID_RE.search(line.get("productDescription") or "")
    if m:
        return m.group(0)
    if "Cloud Router:" not in (line.get("detailedDescription") or ""):
        m = UUID_RE.search(line.get("detailedDescription") or "")
        if m:
            return m.group(0)
    for info in line.get("additionalInfo") or []:
        if info.get("key") == "UUID" and info.get("value"):
            return info["value"]
    return None


def line_category(line: dict) -> str:
    code = line.get("productCode") or ""
    desc = (line.get("productDescription") or "").lower()
    if code.startswith("NEDG") or "network edge" in desc:
        return "Network Edge"
    if "cloud router" in desc and "virtual connection" not in desc:
        return "Cloud Router"
    if "aws" in desc:
        return "AWS"
    if "google" in desc:
        return "Google Cloud"
    if code.startswith("EC0") or "internet access" in desc or "equinix connect" in desc:
        return "Internet Access"
    return "Other"


def line_item(line: dict, uuid: str | None, names: dict) -> str:
    if uuid in names:
        return names[uuid]
    desc = line.get("productDescription") or ""
    m = re.search(r"-(\S+-Node\d)\b", desc)          # Network Edge lines name the node: "... -Lab-FW-Node1 -Metro ..."
    if m:
        return m.group(1)
    return re.split(r"\s+-Recurring Charge", desc)[0].strip(" -") or (line.get("productName") or "(unnamed)")


def amount(line: dict) -> float:
    return float((line.get("recurringAmount") or 0) + (line.get("nonRecurringAmount") or 0) + (line.get("adjustment") or 0))


def _month(v) -> pd.Timestamp:
    return pd.Timestamp(v).to_period("M").to_timestamp()


def normalize(invoices: list[dict], lines: list[dict], names: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Raw invoices + invoice lines -> (lines table, invoices table). Only cost fields are kept: no contacts, bank details or secrets."""
    names = names or {}
    rows = []
    for ln in lines:
        if not ln.get("transactionDate"):
            continue
        val, uuid = amount(ln), line_uuid(ln)
        credit = val < 0 or "CREDIT" in (ln.get("activityType") or "").upper()
        rows.append(dict(month=_month(ln["transactionDate"]), invoice_id=str(ln.get("transactionId") or ""), kind="credit" if credit else "charge",
                         category=line_category(ln), item=line_item(ln, uuid, names), product=ln.get("productName") or "", uuid=uuid or "",
                         activity=ln.get("activityType") or "", amount=round(val, 2), tax=round(float(ln.get("taxAmount") or 0), 2),
                         currency=ln.get("currencyCode") or "USD"))
    inv = [dict(month=_month(i["transactionDate"]), invoice_id=str(i.get("transactionId") or ""), type=i.get("transactionType") or "",
                recurring=float(i.get("totalRecurringAmount") or 0), one_time=float(i.get("totalNonRecurringAmount") or 0),
                total=float(i.get("totalAmount") or 0), currency=i.get("currencyCode") or "USD")
           for i in invoices if i.get("transactionDate")]
    return pd.DataFrame(rows, columns=LINE_COLS), pd.DataFrame(inv, columns=INV_COLS)


# ------------------------------------------------------------------------------------------- store
def read() -> tuple[pd.DataFrame, pd.DataFrame]:
    return platforms.read(LINES, LINE_COLS), platforms.read(INVOICES, INV_COLS)


def save(lines: pd.DataFrame, inv: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    return platforms.merge_months(LINES, lines, LINE_COLS), platforms.merge_months(INVOICES, inv, INV_COLS)


# ------------------------------------------------------------------------------------------- analysis
def monthly(lines: pd.DataFrame) -> pd.DataFrame:
    """Charges by month x category (credits excluded), every category present, oldest first."""
    ch = lines[lines["kind"].eq("charge")]
    if ch.empty:
        return pd.DataFrame(columns=CATEGORIES, index=pd.DatetimeIndex([], name="month"), dtype=float)
    p = ch.pivot_table(index="month", columns="category", values="amount", aggfunc="sum").fillna(0.0)
    return p.reindex(columns=[c for c in CATEGORIES if c in p.columns]).sort_index()


def credits(lines: pd.DataFrame) -> pd.Series:
    return lines[lines["kind"].eq("credit")].groupby("month")["amount"].sum().sort_index()


def latest_month(lines: pd.DataFrame, inv: pd.DataFrame) -> pd.Timestamp:
    real = inv[inv["type"].eq("INVOICE")]
    return (real["month"].max() if len(real) else lines["month"].max())


def summarize(lines: pd.DataFrame, inv: pd.DataFrame) -> dict | None:
    if lines.empty:
        return None
    last = latest_month(lines, inv)
    m = monthly(lines)
    m = m[m.index <= last]
    tot = m.sum(axis=1)
    prev = tot.index[tot.index < last].max() if (tot.index < last).any() else pd.NaT
    win = tot[tot.index > last - pd.DateOffset(months=12)]
    cr = credits(lines)
    cr12 = cr[(cr.index > last - pd.DateOffset(months=12)) & (cr.index <= last)]
    real = inv[inv["type"].eq("INVOICE") & inv["month"].eq(last)]
    run_rate = float(real["recurring"].sum()) if len(real) else float(tot.get(last, 0))
    now = lines[lines["month"].eq(last) & lines["kind"].eq("charge")]
    first = win.index.min()
    return dict(last=last, prev=prev, first=first, first_total=float(win.get(first, 0)), monthly=m, total=tot, now=float(tot.get(last, 0)), before=float(tot.get(prev, 0)) if pd.notna(prev) else float("nan"),
                run_rate=run_rate, last12=float(win.sum()), months=len(win), credits=cr, credits12=float(cr12.sum()), credit_months=cr12[cr12 != 0],
                items=int(now.groupby(["category", "item"]).ngroups), currency=(lines["currency"].mode().iat[0] if len(lines) else "USD"))


def latest_items(lines: pd.DataFrame, last: pd.Timestamp) -> pd.DataFrame:
    """The latest invoice month by item, biggest first (charges only)."""
    now = lines[lines["month"].eq(last) & lines["kind"].eq("charge")]
    g = now.groupby(["category", "item"], as_index=False)["amount"].sum().sort_values("amount", ascending=False)
    g["share"] = g["amount"] / g["amount"].sum() if g["amount"].sum() else 0.0
    return g.reset_index(drop=True)


def changes(lines: pd.DataFrame, last: pd.Timestamp, prev: pd.Timestamp) -> pd.DataFrame:
    """Item by item, latest month vs the month before: New, Ended, Higher, Lower. Unchanged lines are left out."""
    if pd.isna(prev):
        return pd.DataFrame(columns=["category", "item", "before", "now", "change", "status"])
    ch = lines[lines["kind"].eq("charge")]
    a = ch[ch["month"].eq(prev)].groupby(["category", "item"])["amount"].sum().rename("before")
    b = ch[ch["month"].eq(last)].groupby(["category", "item"])["amount"].sum().rename("now")
    x = pd.concat([a, b], axis=1).fillna(0.0).reset_index()
    x["change"] = x["now"] - x["before"]
    x["status"] = "Unchanged"
    x.loc[(x["before"].abs() < 0.5) & (x["now"].abs() >= 0.5), "status"] = "New"
    x.loc[(x["before"].abs() >= 0.5) & (x["now"].abs() < 0.5), "status"] = "Ended"
    x.loc[(x["status"] == "Unchanged") & (x["change"] >= 0.5), "status"] = "Higher"
    x.loc[(x["status"] == "Unchanged") & (x["change"] <= -0.5), "status"] = "Lower"
    x = x[x["status"] != "Unchanged"]
    return x.reindex(x["change"].abs().sort_values(ascending=False).index).reset_index(drop=True)


def pct(a: float, b: float) -> float:
    return float("nan") if not b or pd.isna(b) else a / b - 1


def kpi_items(d: dict) -> list[dict]:
    from .ui import money
    mom = pct(d["now"], d["before"])
    fmt = lambda v: "–" if pd.isna(v) else f"{v:+.1%}"
    return [
        dict(label=f"{d['last']:%b %Y} charges", value=money(d["now"]), neutral=bool(pd.notna(mom) and abs(d["now"] - d["before"]) < 0.5), inverse=True,
             delta=("" if pd.isna(mom) else "no change vs prior month" if abs(d["now"] - d["before"]) < 0.5 else fmt(mom) + " vs prior month"),
             help="Recurring + one-time charges on the latest invoice month, before tax and without credits."),
        dict(label="Run-rate / month", value=money(d["run_rate"]), inverse=True,
             delta=(fmt(pct(d["now"], d["first_total"])) + f" vs {d['first']:%b %Y}") if d["first"] != d["last"] and d["first_total"] else "",
             help="Recurring amount on the latest invoice, compared with the first month of the window."),
        dict(label=f"Last {d['months']} months", value=money(d["last12"]), delta="", help="Charges before tax, without credits."),
        dict(label="Credits (12 mo)", value=money(d["credits12"]), delta="", neutral=True,
             help="One-time refunds and adjustments. Reported on their own: never counted as lower charges or savings."),
        dict(label="Billed lines", value=f"{d['items']:,}", delta="", help="Distinct items charged in the latest invoice month."),
    ]


def findings(d: dict, items: pd.DataFrame, chg: pd.DataFrame) -> list[str]:
    from .ui import money
    out = []
    mom = pct(d["now"], d["before"])
    if pd.notna(mom) and abs(d["now"] - d["before"]) < 0.5:
        out.append(f"{d['last']:%B %Y} charges are **{money(d['now'])}**, unchanged from {d['prev']:%B}.")
    elif pd.notna(mom):
        out.append(f"{d['last']:%B %Y} charges are **{money(d['now'])}**, {'up' if mom > 0 else 'down'} **{abs(mom):.1%}** ({money(d['now'] - d['before'])}) on {d['prev']:%B}.")
    else:
        out.append(f"{d['last']:%B %Y} charges are **{money(d['now'])}**.")
    if len(items):
        top = items.iloc[0]
        top3 = items["share"].head(3).sum()
        out.append(f"The biggest line is **{top['item']}** ({top['category']}) at {money(top['amount'])}, {top['share']:.0%} of the month. The top 3 lines are {top3:.0%}.")
        cat = items.groupby("category")["amount"].sum().sort_values(ascending=False)
        out.append(f"By category, **{cat.index[0]}** leads with {cat.iloc[0] / cat.sum():.0%} of the month.")
    new, ended = chg[chg["status"].eq("New")], chg[chg["status"].eq("Ended")]
    if len(new):
        out.append(f"**{len(new)} new line(s)** since {d['prev']:%B}, adding {money(new['now'].sum())} a month: " + ", ".join(new["item"].head(3)) + ("…" if len(new) > 3 else "") + ".")
    if len(ended):
        out.append(f"**{len(ended)} line(s) ended** since {d['prev']:%B}, removing {money(ended['before'].sum())} a month: " + ", ".join(ended["item"].head(3)) + ("…" if len(ended) > 3 else "") + ".")
    if d["credits12"]:
        out.append(f"{money(abs(d['credits12']))} of **credits** in the last 12 months across {len(d['credit_months'])} month(s). Credits are refunds or adjustments, not lower charges, so they are not counted as savings.")
    return out


# ------------------------------------------------------------------------------------------- charts
def fig_monthly(d: dict) -> go.Figure:
    m = d["monthly"].tail(13)
    fig = go.Figure()
    for c in m.columns:
        fig.add_bar(x=m.index, y=m[c], name=c, marker_color=COLORS.get(c, "#94A3B8"), hovertemplate="%{x|%b %Y}<br>" + c + ": $%{y:,.0f}<extra></extra>")
    fig.add_scatter(x=m.index, y=m.sum(axis=1), mode="text", text=[f"${v / 1e3:,.0f}K" for v in m.sum(axis=1)], textposition="top center", showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", height=380, yaxis=dict(tickformat="$,.0f", title="Charges per month"),
                      xaxis=dict(tickformat="%b %Y"), legend=dict(orientation="h", y=-0.15), margin=dict(t=30))
    return fig


def fig_items(items: pd.DataFrame, top: int = 12) -> go.Figure:
    t = items.head(top).iloc[::-1]
    fig = go.Figure(go.Bar(x=t["amount"], y=t["item"], orientation="h", marker_color=[COLORS.get(c, "#94A3B8") for c in t["category"]],
                           customdata=t["category"], hovertemplate="%{y}<br>%{customdata}: $%{x:,.0f}<extra></extra>"))
    fig.update_layout(height=max(260, 28 * len(t) + 80), xaxis=dict(tickformat="$,.0f", nticks=3, tickangle=0), margin=dict(t=20, l=10))
    return fig


def fig_credits(d: dict) -> go.Figure | None:
    c = d["credits"][d["credits"].index > d["last"] - pd.DateOffset(months=12)]
    c = c[c != 0]
    if c.empty:
        return None
    fig = go.Figure(go.Bar(x=[f"{m:%b %Y}" for m in c.index], y=c.values, marker_color="#15803d", width=0.35,
                           hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"))
    fig.update_layout(height=200, yaxis=dict(tickformat="$,.0f", title="Credits"), xaxis=dict(type="category"), margin=dict(t=10, b=30))
    return fig


# ------------------------------------------------------------------------------------------- display tables
def items_display(items: pd.DataFrame) -> pd.DataFrame:
    from .ui import money
    return pd.DataFrame({"Category": items["category"], "Item": items["item"], "Month": items["amount"].map(money), "Share": items["share"].map("{:.1%}".format)})


def changes_display(chg: pd.DataFrame) -> pd.DataFrame:
    from .ui import money
    return pd.DataFrame({"Status": chg["status"], "Category": chg["category"], "Item": chg["item"], "Before": chg["before"].map(money),
                         "Now": chg["now"].map(money), "Change": chg["change"].map(money)})


def monthly_display(d: dict, rows: int = 13) -> pd.DataFrame:
    m = d["monthly"].tail(rows).copy()
    m["Total"] = m.sum(axis=1)
    m["Credits"] = d["credits"].reindex(m.index).fillna(0.0)
    m.index = m.index.strftime("%b %Y")
    return m.iloc[::-1]
