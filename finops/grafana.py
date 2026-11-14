"""Grafana Cloud spend: tables, commit runway, reconciliation and the analysis behind the page and the report. No network calls here.

Grafana is an independent platform: never added to the AWS / GCP totals, budgets or savings.

Cost model (checked by the standalone Grafana project against the commit-balance drawdown): ``grafanacloud_org_<product>_overage`` metrics
are running USD amounts for the billing month and reset at the cycle start, so the LAST value of a month is that month's cost, and the
per-product values add up to ``total_overage``. These are Grafana-reported usage costs, not invoices. The current month is a running
estimate and is always labelled as one."""
from __future__ import annotations
import re
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go

from . import platforms

PLATFORM = "Grafana"
COSTS, DRIVER_T, BALANCE, FACTS = "grafana_costs", "grafana_drivers", "grafana_balance", "grafana_facts"
LOGS, ADAPT = "grafana_logs", "grafana_adaptive"
LOG_COLS = ["day", "service", "gib"]
LOG_DAYS = 31                                          # Loki's index-volume lookback
MIN_LOG_DAYS = 14                                      # fewer covered days and the per-service view is omitted
OTHER_SERVICES = "Other services (combined)"
ADAPT_COLS = ["metric", "action", "raw", "current", "recommended", "drop_labels", "aggregations", "in_rules", "in_dashboards", "in_queries"]
COST_COLS = ["month", "component", "product", "cost"]
DRV_COLS = ["month", "driver", "unit", "value"]
BAL_COLS = ["month", "balance"]                       # "month" holds the day: platforms.merge_months replaces by that column

PRODUCT_RULES = [(r"^metrics", "Metrics"), (r"^logs", "Logs"), (r"^traces", "Traces"), (r"^profiles", "Profiles"), (r"^k6", "k6"), (r"^irm", "IRM"),
                 (r"^grafana", "Grafana users"), (r"^infra_o11y", "Infrastructure"), (r"^(app|fe)_o11y", "Application / frontend"),
                 (r"^sm", "Synthetics"), (r"^(assistant|ai_tokens)", "AI")]
PRODUCTS = [p for _, p in PRODUCT_RULES] + ["Other"]
COLORS = {"Metrics": "#2563EB", "Logs": "#0F766E", "Traces": "#7C3AED", "Profiles": "#0891B2", "k6": "#CA8A04", "IRM": "#BE185D",
          "Grafana users": "#E07B00", "Infrastructure": "#475569", "Application / frontend": "#16A34A", "Synthetics": "#9333EA",
          "AI": "#DC2626", "Other": "#94A3B8"}

# daily volume drivers: name -> (unit, PromQL returning the daily value)
DRIVERS = {
    "Active series": ("series", "sum(avg_over_time(grafanacloud_instance_active_series[1d]))"),
    "Logs ingested": ("GB / day", "sum(avg_over_time(grafanacloud_logs_instance_billable_bytes_received_per_second[1d])) * 86400 / 1073741824"),
    "Traces ingested": ("GB / day", "sum(avg_over_time(grafanacloud_traces_instance_bytes_received_per_second[1d])) * 86400 / 1073741824"),
    "Billable users": ("users", "sum(avg_over_time(grafanacloud_org_grafana_billable_users[1d]))"),
}


def money(v: float) -> str:
    """Like the dashboard's money(), but one decimal in the tens of thousands: a $29.5K month must not read as $30K."""
    if pd.isna(v):
        return "–"
    a = abs(v)
    s = f"${a / 1e6:,.2f}M" if a >= 999_500 else f"${a / 1e3:,.1f}K" if a >= 1e4 else f"${a:,.0f}"
    return "-" + s if v < 0 else s


def product_for(component: str) -> str:
    for pattern, product in PRODUCT_RULES:
        if re.match(pattern, component):
            return product
    return "Other"


# ------------------------------------------------------------------------------------------- store
def read() -> dict:
    return dict(costs=platforms.read(COSTS, COST_COLS), drivers=platforms.read(DRIVER_T, DRV_COLS), balance=platforms.read(BALANCE, BAL_COLS),
                facts=platforms.read_json(FACTS), logs=platforms.read(LOGS, LOG_COLS), adaptive=platforms.read(ADAPT, ADAPT_COLS))


def save(costs: pd.DataFrame, drivers: pd.DataFrame, balance: pd.DataFrame, facts: dict, logs: pd.DataFrame | None = None, adaptive: pd.DataFrame | None = None) -> None:
    platforms.merge_months(COSTS, costs, COST_COLS)
    platforms.merge_months(DRIVER_T, drivers, DRV_COLS)
    platforms.merge_months(BALANCE, balance, BAL_COLS)
    if logs is not None and len(logs):
        platforms.merge_months(LOGS, logs, LOG_COLS, month_col="day")          # days accumulate beyond Loki's 31-day window
    if adaptive is not None:
        platforms.write(ADAPT, adaptive)                                          # a snapshot: today's recommendations replace the last ones
    platforms.write_json(FACTS, facts)


# ------------------------------------------------------------------------------------------- analysis
def monthly(costs: pd.DataFrame) -> pd.DataFrame:
    """Cost by complete month x product (the ``total`` reference metric is excluded), oldest first."""
    c = costs[costs["component"].ne("total")]
    if c.empty:
        return pd.DataFrame(columns=PRODUCTS, index=pd.DatetimeIndex([], name="month"), dtype=float)
    p = c.pivot_table(index="month", columns="product", values="cost", aggfunc="sum").fillna(0.0)
    return p.reindex(columns=[x for x in PRODUCTS if x in p.columns]).sort_index()


def reported_total(costs: pd.DataFrame) -> pd.Series:
    return costs[costs["component"].eq("total")].set_index("month")["cost"].sort_index()


def reconcile(computed: float, billed: float, tolerance: float = 2.0) -> dict:
    var = (0.0 if computed == 0 else float("inf")) if billed == 0 else (computed - billed) / billed * 100.0
    return dict(cost=computed, drawdown=billed, variance=var, ok=abs(var) <= tolerance)


def reconcile_with_commit(balance: pd.DataFrame, totals: pd.Series, tolerance: float = 2.0) -> pd.DataFrame:
    """The commit balance steps down shortly after a month closes by about that month's final cost: compare the two."""
    if balance.empty or totals.empty:
        return pd.DataFrame(columns=["month", "cost", "drawdown", "variance", "ok"])
    bal = balance.set_index("month")["balance"].sort_index()
    drops = bal.diff().where(bal.diff() < -1.0).dropna()
    rows = []
    for month, cost in totals.items():
        nxt = pd.Timestamp(month) + pd.offsets.MonthBegin(1)
        w = drops[(drops.index >= nxt) & (drops.index < nxt + pd.Timedelta(days=10))]
        if not w.empty:
            rows.append(dict(month=month, **reconcile(float(cost), float(-w.sum()), tolerance)))
    return pd.DataFrame(rows, columns=["month", "cost", "drawdown", "variance", "ok"])


def commit_status(total: float, balance: float, monthly_totals: pd.Series, as_of: date, contract_end: date | None, window: int = 3) -> dict | None:
    """How long the remaining prepaid commit lasts at the recent burn. None when there is too little history."""
    recent = monthly_totals.tail(window)
    if not total or len(recent) < 2 or recent.mean() <= 0:
        return None
    burn = float(recent.mean())
    runway = balance / burn
    depletion = as_of + timedelta(days=round(runway * 30.4375))
    short = None
    if contract_end:
        left = (contract_end - as_of).days / 30.4375
        short = max(left - runway, 0.0) or None
    return dict(total=total, balance=balance, consumed=1 - balance / total, burn=burn, runway=runway, depletion=depletion, contract_end=contract_end,
                shortfall=short, months=len(recent))


def _d(v) -> date | None:
    return date.fromisoformat(v) if v else None


def summarize(data: dict, budget: float = 0.0, tolerance: float = 2.0) -> dict | None:
    costs, facts = data["costs"], data["facts"]
    m = monthly(costs)
    if m.empty:
        return None
    tot = m.sum(axis=1)
    last = tot.index.max()
    prev = tot.index[tot.index < last].max() if (tot.index < last).any() else pd.NaT
    win = tot[tot.index > last - pd.DateOffset(months=12)]
    as_of = _d(facts.get("as_of")) or date.today()
    cs = commit_status(facts.get("commit_total") or 0.0, facts.get("commit_balance") or 0.0, tot, as_of, _d(facts.get("contract_end")))
    rec = reconcile_with_commit(data["balance"], reported_total(costs), tolerance)
    return dict(monthly=m, total=tot, last=last, prev=prev, now=float(tot[last]), before=float(tot.get(prev, float("nan"))) if pd.notna(prev) else float("nan"),
                last12=float(win.sum()), months=len(win), budget=budget, estimate=facts.get("current_estimate"), est_month=facts.get("current_month"),
                commit=cs, reconciliation=rec, facts=facts, drivers=data["drivers"], balance=data["balance"], logs=data.get("logs"), adaptive=data.get("adaptive"),
                parts_vs_total=_parts_vs_total(costs, tolerance))


def _parts_vs_total(costs: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    parts = costs[costs["component"].ne("total")].groupby("month")["cost"].sum()
    tot = reported_total(costs)
    x = pd.concat([parts.rename("parts"), tot.rename("reported")], axis=1).dropna()
    x["diff"] = x["parts"] - x["reported"]
    x["ok"] = x["diff"].abs() <= max(tolerance / 100.0, 0.001) * x["reported"].abs().clip(lower=1)
    return x


def pct(a: float, b: float) -> float:
    return float("nan") if not b or pd.isna(b) else a / b - 1


def product_table(d: dict) -> pd.DataFrame:
    """Last complete month by product with the 3-month average, change vs the month before and share."""
    m = d["monthly"]
    last, prev = d["last"], d["prev"]
    t = pd.DataFrame({"now": m.loc[last], "avg3": m.tail(3).mean(), "before": m.loc[prev] if pd.notna(prev) else float("nan"), "last12": m.tail(12).sum()})
    t = t[(t["now"].abs() > 0.5) | (t["last12"].abs() > 0.5)]
    t["change"] = t["now"] - t["before"]
    t["share"] = t["now"] / t["now"].sum() if t["now"].sum() else 0.0
    return t.sort_values("now", ascending=False)


def kpi_items(d: dict) -> list[dict]:
    mom = pct(d["now"], d["before"])
    flat = pd.notna(mom) and abs(d["now"] - d["before"]) < 0.5
    cs = d["commit"]
    out = [
        dict(label=f"{d['last']:%b %Y} cost", value=money(d["now"]), neutral=bool(flat), inverse=True,
             delta="" if pd.isna(mom) else "no change vs prior month" if flat else f"{mom:+.1%} vs prior month",
             help="Grafana-reported usage cost for the last complete month (not an invoice)."),
        dict(label=f"{pd.Period(d['est_month']).start_time:%b %Y} so far (est.)" if d["estimate"] is not None and d["est_month"] else "Current month", value=money(d["estimate"]) if d["estimate"] is not None else "–", delta="",
             help="Running estimate for the month in progress. It grows until month end and resets at the next cycle."),
        dict(label=f"Last {d['months']} months", value=money(d["last12"]), delta="",
             help="Complete months only."),
    ]
    if cs:
        out += [dict(label="Commit remaining", value=money(cs["balance"]), delta=f"{cs['consumed']:.0%} used", neutral=True, help=f"Prepaid commit of {money(cs['total'])}."),
                dict(label="Runway", value=f"{cs['runway']:.1f} mo", delta=f"to {cs['depletion']:%b %Y}", neutral=True,
                     help=f"At the last {cs['months']}-month average of {money(cs['burn'])} a month.")]
    else:
        out += [dict(label="Budget / month", value=money(d["budget"]) if d["budget"] else "–", delta="", help="config.toml [grafana] monthly_budget."),
                dict(label="Products billed", value=str(int((d["monthly"].loc[d["last"]] > 0.5).sum())), delta="")]
    return out


def findings(d: dict, prod: pd.DataFrame) -> list[str]:
    out = []
    mom = pct(d["now"], d["before"])
    if pd.notna(mom) and abs(d["now"] - d["before"]) < 0.5:
        out.append(f"{d['last']:%B %Y} cost is **{money(d['now'])}**, unchanged from {d['prev']:%B}.")
    elif pd.notna(mom):
        out.append(f"{d['last']:%B %Y} cost is **{money(d['now'])}**, {'up' if mom > 0 else 'down'} **{abs(mom):.1%}** ({money(d['now'] - d['before'])}) on {d['prev']:%B}.")
    else:
        out.append(f"{d['last']:%B %Y} cost is **{money(d['now'])}**.")
    if d["budget"]:
        gap = d["budget"] - d["now"]
        out.append(f"Against the **{money(d['budget'])}** monthly budget that is {money(abs(gap))} {'under' if gap >= 0 else 'over'} ({d['now'] / d['budget']:.0%} of budget).")
    if len(prod):
        top = prod.iloc[0]
        out.append(f"**{prod.index[0]}** is the largest product at {money(top['now'])} ({top['share']:.0%} of the month). "
                   + (f"Top 3 products: {prod['share'].head(3).sum():.0%}." if len(prod) > 3 else ""))
        mv = prod["change"].dropna()
        if len(mv) and mv.abs().max() >= 0.5:
            k = mv.abs().idxmax()
            out.append(f"Biggest mover vs {d['prev']:%B}: **{k}** ({'+' if mv[k] > 0 else '-'}{money(abs(mv[k]))}).")
    if d["estimate"] is not None and d["estimate"] > 0 and d["est_month"]:
        out.append(f"{pd.Period(d['est_month']).start_time:%B %Y} is running at **{money(d['estimate'])}** so far (an estimate that grows until month end).")
    cs = d["commit"]
    if cs:
        msg = f"**{money(cs['balance'])}** of the {money(cs['total'])} commit remains ({cs['consumed']:.0%} used); at the last {cs['months']}-month average of {money(cs['burn'])} it lasts {cs['runway']:.1f} months (to about {cs['depletion']:%b %Y})"
        if cs["contract_end"]:
            msg += f", and the contract ends {cs['contract_end']:%b %Y}" + (f" — about {cs['shortfall']:.1f} months of spend would fall beyond the commit." if cs["shortfall"] else ", so the commit covers the term.")
        else:
            msg += "."
        out.append(msg)
    return out



# ------------------------------------------------------------------------------------------- logs by service
def log_view(logs: pd.DataFrame | None, top: int = 8, recent_days: int = 7) -> dict | None:
    """Who produces the logs, from Loki index volume (STORED bytes): relative shares over the days Loki still holds, never a billed quantity.
    None when fewer than MIN_LOG_DAYS days are covered. Movers compare the last `recent_days` with the days before them."""
    if logs is None or logs.empty:
        return None
    m = logs.pivot_table(index="day", columns="service", values="gib", aggfunc="sum").fillna(0.0).sort_index()
    m = m[m.sum(axis=1) > 0]
    if len(m) < MIN_LOG_DAYS:
        return None
    totals = m.sum().sort_values(ascending=False)
    grand = float(totals.sum())
    rec, base = m.tail(recent_days), m.iloc[:-recent_days]
    rows = [dict(service=str(k), share=float(v) / grand, per_day=float(v) / len(m)) for k, v in totals.head(top).items()]
    rest = float(totals.iloc[top:].sum())
    if rest > 0:
        rows.append(dict(service=OTHER_SERVICES, share=rest / grand, per_day=rest / len(m)))
    movers = pd.DataFrame(columns=["service", "extra_per_day", "share_of_increase"])
    if len(base) >= recent_days:
        delta = (rec.mean() - base.mean()).clip(lower=0).sort_values(ascending=False)
        if delta.sum() > 0:
            movers = pd.DataFrame({"service": delta.index, "extra_per_day": delta.values, "share_of_increase": delta.values / delta.sum()}).head(5)
            movers = movers[movers["extra_per_day"] > 0]
    order = list(totals.head(5).index)
    daily = m[order].copy()
    daily["Other services"] = m.drop(columns=order).sum(axis=1)
    return dict(days=len(m), first=m.index.min(), last=m.index.max(), table=pd.DataFrame(rows), movers=movers, daily=daily, total_per_day=float(grand / len(m)),
                recent=float(rec.sum(axis=1).mean()), earlier=float(base.sum(axis=1).mean()) if len(base) else float("nan"), services=int(m.shape[1]))


def log_findings(v: dict) -> list[str]:
    out = []
    t = v["table"]
    top = t[t["service"].ne(OTHER_SERVICES)].head(3)
    if len(top):
        out.append(f"**{top.iloc[0]['service']}** produces the most log volume ({top.iloc[0]['share']:.0%}); the top {len(top)} services are {top['share'].sum():.0%} of it, out of {v['services']:,} services.")
    if pd.notna(v["earlier"]) and v["earlier"] > 0:
        chg = v["recent"] / v["earlier"] - 1
        out.append(f"The last 7 days average {v['recent']:.0f} GiB a day, {'up' if chg > 0 else 'down'} {abs(chg):.0%} on the {v['days'] - 7} days before.")
    mv = v["movers"]
    if len(mv):
        out.append("Biggest increases: " + ", ".join(f"**{r.service}** (+{r.extra_per_day:.1f} GiB/day, {r.share_of_increase:.0%} of the rise)" for r in mv.head(3).itertuples()) + ".")
    return out


# ------------------------------------------------------------------------------------------- Adaptive Metrics
def adaptive_view(ad: pd.DataFrame | None, active_now: float | None, top: int = 10) -> dict | None:
    """Grafana's own per-metric recommendations. The total is usually small next to the active series (existing aggregation rules already did most of the work)."""
    if ad is None or ad.empty:
        return None
    a = ad.copy()
    a["reducible"] = (a["current"] - a["recommended"]).clip(lower=0)
    a["used"] = a["in_rules"] + a["in_dashboards"] + a["in_queries"]
    red = int(a["reducible"].sum())
    t = a[a["reducible"] > 0].sort_values("reducible", ascending=False)
    return dict(metrics=len(a), raw=int(a["raw"].sum()), current=int(a["current"].sum()), recommended=int(a["recommended"].sum()), reducible=red,
                pct_of_active=(red / active_now) if active_now else float("nan"), top=t.head(top), top_share=float(t["reducible"].head(top).sum() / red) if red else 0.0,
                unused_series=int(t[t["used"] == 0]["reducible"].sum()), n_candidates=len(t))


def adaptive_display(top: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Metric": top["metric"].values, "Current series": top["current"].map("{:,}".format).values, "Recommended": top["recommended"].map("{:,}".format).values,
                         "Reducible": top["reducible"].map("{:,}".format).values,
                         "Labels to drop": top["drop_labels"].map(lambda x: ", ".join(x.split(",")[:3]) + (" …" if len(x.split(",")) > 3 else "")).values,
                         "Used in": top.apply(lambda r: f"{r['in_dashboards']} dashboards · {r['in_rules']} rules · {r['in_queries']} queries", axis=1).values})


# ------------------------------------------------------------------------------------------- unit costs
def unit_costs(d: dict) -> pd.DataFrame | None:
    """Cost per unit of volume by complete month: separates 'we used more' from 'it got more expensive'. Metrics are billed on the 95th percentile,
    so that row (cost per average 1K active series) is approximate."""
    dr = d["drivers"]
    if dr is None or dr.empty:
        return None
    v = dr.pivot_table(index="month", columns="driver", values="value", aggfunc="mean")
    m = d["monthly"]
    idx = m.index.intersection(v.index)
    if idx.empty:
        return None
    days = pd.Series(idx.days_in_month, index=idx)
    u = pd.DataFrame(index=idx)
    if "Active series" in v and "Metrics" in m:
        u["Metrics $ per 1K series (approx.)"] = m.loc[idx, "Metrics"] / (v.loc[idx, "Active series"] / 1000)
    if "Logs ingested" in v and "Logs" in m:
        u["Logs $ per GB"] = m.loc[idx, "Logs"] / (v.loc[idx, "Logs ingested"] * days)
    if "Billable users" in v and "Grafana users" in m:
        u["Grafana users $ per user"] = m.loc[idx, "Grafana users"] / v.loc[idx, "Billable users"]
    return u.replace([float("inf")], float("nan")).dropna(how="all")


def unit_note(d: dict, u: pd.DataFrame | None) -> str | None:
    """One sentence on whether the latest logs jump is volume or price."""
    if u is None or "Logs $ per GB" not in u or len(u) < 2 or d["last"] not in u.index or pd.isna(d["prev"]) or d["prev"] not in u.index:
        return None
    p0, p1 = u.loc[d["prev"], "Logs $ per GB"], u.loc[d["last"], "Logs $ per GB"]
    c0, c1 = d["monthly"].loc[d["prev"], "Logs"], d["monthly"].loc[d["last"], "Logs"]
    if pd.isna(p0) or pd.isna(p1) or not p0 or not c0 or abs(c1 / c0 - 1) < 0.15:
        return None
    flat = abs(p1 / p0 - 1) < 0.03
    head = f"Logs cost moved {c1 / c0 - 1:+.0%} ({money(c0)} to {money(c1)}) "
    if flat:
        return head + f"while the price per GB stayed at ${p1:.2f}: the price is flat, so the change is **volume**, not a price rise."
    return head + f"while the price per GB went from ${p0:.2f} to ${p1:.2f}: the price moved as well, so it is not only volume."


# ------------------------------------------------------------------------------------------- charts
def fig_monthly(d: dict, rows: int = 13) -> go.Figure:
    m = d["monthly"].tail(rows)
    fig = go.Figure()
    for c in m.columns:
        fig.add_bar(x=[f"{x:%b %y}" for x in m.index], y=m[c], name=c, marker_color=COLORS.get(c, "#94A3B8"), hovertemplate="%{x}<br>" + c + ": $%{y:,.0f}<extra></extra>")
    labels = [f"{x:%b %y}" for x in m.index]
    top = float(m.sum(axis=1).max())
    if d["estimate"] is not None and d["est_month"]:
        em = pd.Period(d["est_month"]).start_time
        if em > m.index.max():
            labels.append(f"{em:%b %y} est.")
            top = max(top, float(d["estimate"]))
            fig.add_bar(x=[labels[-1]], y=[d["estimate"]], name="Month in progress (est.)", marker_color="rgba(100,116,139,0.35)", marker_line=dict(color="#64748B", width=1.5),
                        hovertemplate="%{x}<br>running estimate: $%{y:,.0f}<extra></extra>")
    if d["budget"]:
        fig.add_hline(y=d["budget"], line_dash="dash", line_color="#B91C1C", annotation_text=f"Budget ${d['budget'] / 1e3:,.0f}K", annotation_position="top left")
    fig.add_scatter(x=labels[:len(m)], y=m.sum(axis=1), mode="text", text=[f"${v / 1e3:,.1f}K" for v in m.sum(axis=1)], textposition="top center", showlegend=False, hoverinfo="skip")
    top = max(top, d["budget"] or 0)
    fig.update_layout(barmode="stack", height=400, yaxis=dict(tickformat="$,.0f", title="Cost per month", range=[0, top * 1.14]), xaxis=dict(type="category", tickangle=0),
                      legend=dict(orientation="h", y=-0.14, yanchor="top"), margin=dict(t=20, b=90))
    return fig


def fig_commit(d: dict) -> go.Figure | None:
    b = d["balance"]
    cs = d["commit"]
    if b.empty or not cs:
        return None
    b = b.sort_values("month")
    fig = go.Figure(go.Scatter(x=b["month"], y=b["balance"], mode="lines", line=dict(color="#2563EB", width=2.5), fill="tozeroy", fillcolor="rgba(37,99,235,0.10)",
                               name="Commit balance", hovertemplate="%{x|%b %d, %Y}: $%{y:,.0f}<extra></extra>"))
    today = pd.Timestamp(d["facts"].get("as_of") or date.today())
    fut = pd.date_range(today, cs["depletion"], periods=12) if cs["depletion"] > today.date() else []
    if len(fut):
        fig.add_scatter(x=fut, y=[cs["balance"] * (1 - i / (len(fut) - 1)) for i in range(len(fut))], mode="lines", line=dict(color="#64748B", dash="dot"), name="At recent burn",
                        hovertemplate="%{x|%b %Y}: $%{y:,.0f}<extra></extra>")
    if cs["contract_end"]:
        fig.add_vline(x=pd.Timestamp(cs["contract_end"]).timestamp() * 1000, line_dash="dash", line_color="#B91C1C", annotation_text="Contract end", annotation_position="top left")
    fig.update_layout(height=320, yaxis=dict(tickformat="$,.0f", title="Remaining commit"), xaxis=dict(tickformat="%b %y", nticks=5, tickangle=0), legend=dict(orientation="h", y=-0.25), margin=dict(t=30))
    return fig


def fig_drivers(d: dict) -> go.Figure | None:
    dr = d["drivers"]
    if dr.empty:
        return None
    names = [n for n in DRIVERS if n in set(dr["driver"])]
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=1, cols=len(names), subplot_titles=names)
    for i, n in enumerate(names, 1):
        x = dr[dr["driver"].eq(n)].sort_values("month").tail(13)
        fig.add_scatter(x=x["month"], y=x["value"], mode="lines+markers", line=dict(color="#2563EB"), showlegend=False, row=1, col=i,
                        hovertemplate="%{x|%b %Y}: %{y:,.1f}<extra></extra>")
        fig.update_xaxes(tickformat="%b %y", nticks=4, row=1, col=i)
        fig.update_yaxes(tickformat=",.3~s", rangemode="tozero", row=1, col=i)
    fig.update_layout(height=260, margin=dict(t=40))
    fig.update_annotations(font_size=12)
    return fig


def fig_log_services(v: dict) -> go.Figure:
    t = v["table"].iloc[::-1]
    fig = go.Figure(go.Bar(x=t["share"], y=t["service"], orientation="h", marker_color=["#94A3B8" if s == OTHER_SERVICES else "#0F766E" for s in t["service"]],
                           hovertemplate="%{y}: %{x:.1%} of log volume<extra></extra>"))
    fig.update_layout(height=max(260, 28 * len(t) + 80), xaxis=dict(tickformat=".0%", nticks=5), margin=dict(t=20, l=10))
    return fig


def fig_log_daily(v: dict) -> go.Figure:
    dly = v["daily"]
    pal = ["#0F766E", "#2563EB", "#7C3AED", "#E07B00", "#BE185D", "#94A3B8"]
    fig = go.Figure()
    for c, col in zip(dly.columns, pal):
        fig.add_bar(x=dly.index, y=dly[c], name=str(c)[:28], marker_color=col, hovertemplate="%{x|%b %d}: %{y:,.1f} GiB<extra>" + str(c)[:28] + "</extra>")
    fig.update_layout(barmode="stack", height=300, yaxis=dict(title="GiB / day", tickformat=",.0f"), xaxis=dict(tickformat="%b %d"),
                      legend=dict(orientation="h", y=-0.2), margin=dict(t=20))
    return fig


def fig_activity(act: dict, top: int = 10) -> go.Figure | None:
    f = act.get("folders_30d") or {}
    if not f:
        return None
    s = pd.Series(f).sort_values(ascending=False).head(top).iloc[::-1]
    fig = go.Figure(go.Bar(x=s.values, y=s.index, orientation="h", marker_color="#0F766E", hovertemplate="%{y}: %{x:,} events<extra></extra>"))
    fig.update_layout(height=max(240, 26 * len(s) + 80), xaxis=dict(tickformat=",.3~s", nticks=4), margin=dict(t=20, l=10))
    return fig


# ------------------------------------------------------------------------------------------- display tables
def product_display(t: pd.DataFrame, last: pd.Timestamp, prev) -> pd.DataFrame:
    return pd.DataFrame({"Product": t.index, f"{last:%b %Y}": t["now"].map(money).values, "3-mo avg": t["avg3"].map(money).values,
                         "Change": t["change"].map(money).values, "Share": t["share"].map("{:.1%}".format).values, "12 months": t["last12"].map(money).values})


def monthly_display(d: dict, rows: int = 13) -> pd.DataFrame:
    m = d["monthly"].tail(rows).copy()
    m["Total"] = m.sum(axis=1)
    m.index = m.index.strftime("%b %Y")
    return m.iloc[::-1]


def reconciliation_display(rec: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Month": rec["month"].dt.strftime("%b %Y"), "Reported cost": rec["cost"].map(money), "Commit drawdown": rec["drawdown"].map(money),
                         "Variance": rec["variance"].map("{:+.2f}%".format), "Within 2%": rec["ok"].map({True: "Yes", False: "No"})})


def log_display(v: dict) -> pd.DataFrame:
    t = v["table"]
    return pd.DataFrame({"Service": t["service"].values, "Share": t["share"].map("{:.1%}".format).values, "GiB / day (index)": t["per_day"].map("{:,.1f}".format).values})


def unit_display(u: pd.DataFrame, rows: int = 7) -> pd.DataFrame:
    x = u.tail(rows).iloc[::-1].copy()
    x.index = x.index.strftime("%b %Y")
    return x
