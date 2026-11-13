"""Places worth a conversation: non-prod that is as big as prod, steady always-on lines, GCP credits. NO Streamlit.

These are LEADS, not savings: each one is spend in question that its owner has to confirm. Cost data cannot see utilization, existing
reservations or why something runs, so nothing here is a promised saving."""
from __future__ import annotations
import pandas as pd
import plotly.graph_objects as go

from . import dims as D
from .owners import MISSING
from .ui import COLORS, TEMPLATE, money

SKIP_SERVICES = ("Support", "Savings Plans", "Tax", "Refund")
HINTS = (("Relational Database", "reserved instances"), ("ElastiCache", "reserved nodes"), ("OpenSearch", "reserved instances"), ("Redshift", "reserved nodes"),
         ("Elastic Compute Cloud", "a Compute Savings Plan or reserved instances"), ("EC2", "a Compute Savings Plan or reserved instances"),
         ("CloudFront", "a CloudFront savings bundle"), ("BigQuery", "a BigQuery capacity commitment (slots)"), ("Compute Engine", "a committed use discount"),
         ("Cloud SQL", "a committed use discount"), ("Kubernetes", "a committed use discount"))


def nonprod_heavy(df: pd.DataFrame, min_nonprod: float = 20_000) -> pd.DataFrame:
    """Project codes whose non-prod spend is at least their prod spend (latest 12 months). Spend whose environment is unspecified is shown too:
    it may be prod, so a project with a lot of it is a weaker lead."""
    e = D.env_by_code(df)
    e = e[(e["non-prod"] >= min_nonprod) & (e["non-prod"] >= e["prod"]) & (e.index != MISSING["project_code"])]
    cur, _, _ = D.windows(df)
    notes = D._notes(cur, "project_code")
    out = e.assign(notes=e.index.map(notes).fillna("")).sort_values("non-prod", ascending=False)
    out.index.name = "project_code"
    return out


def nonprod_display(t: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Project code": t.index, "Non-prod /12 mo": t["non-prod"].map(money).values, "Prod /12 mo": t["prod"].map(money).values,
                         "Unspecified /12 mo": t["unspecified"].map(money).values,
                         "Non-prod, last 6 mo vs 6 before": t["nonprod_6m_change"].map(D.chg).values, "What it is": t["notes"].values}).reset_index(drop=True)


def _hint(service: str) -> str:
    for k, v in HINTS:
        if k.lower() in service.lower():
            return v
    return "a reservation or commitment, if one does not already cover it"


def steady_lines(df: pd.DataFrame, min_monthly: float = 5_000, max_var: float = 0.08) -> pd.DataFrame:
    """Provider x account x service lines that billed every one of the last 12 months, at least `min_monthly` on average, varying less than `max_var`
    (std / mean): always-on spend, the kind a commitment discount fits. Support, Savings Plans, tax and refunds are not candidates."""
    cur, _, _ = D.windows(df)
    c = cur[~cur["service"].str.contains("|".join(SKIP_SERVICES), case=False)]
    m = c.groupby(["provider", "account", "service", "date"]).cost_net.sum().unstack("date")
    if m.shape[1] < 12:
        return pd.DataFrame(columns=["provider", "account", "service", "avg", "variation", "annual", "project_code", "hint"])
    m = m.dropna()
    m = m[(m.mean(axis=1) >= min_monthly) & (m.min(axis=1) > 0)]
    var = (m.std(axis=1) / m.mean(axis=1))
    m, var = m[var < max_var], var[var < max_var]
    code = c.groupby(["provider", "account"]).project_code.agg(lambda s: s.value_counts().index[0])
    out = pd.DataFrame({"avg": m.mean(axis=1), "variation": var, "annual": m.sum(axis=1)}).reset_index()
    out["project_code"] = [code.get((p, a), "") for p, a in zip(out.provider, out.account)]
    out["hint"] = out["service"].map(_hint)
    return out.sort_values("annual", ascending=False).reset_index(drop=True)


def steady_display(t: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Provider": t.provider, "Account / project": t.account.str.slice(0, 34), "Service": t.service.str.slice(0, 40), "Project code": t.project_code,
                         "Average /mo": t.avg.map(money), "Variation": t.variation.map(lambda v: f"{v:.0%}"), "Last 12 mo": t.annual.map(money),
                         "Worth asking about": t.hint})


def orphans_billing(df: pd.DataFrame, min_monthly: float = 100.0) -> pd.DataFrame:
    """Accounts / projects whose status says Orphan, Deleted or Remove but that still bill: either the status is wrong or something is still running."""
    if "status" not in df:
        return pd.DataFrame(columns=["provider", "account", "status", "avg3", "last12"])
    d = df[df["status"].isin(["Orphan", "Deleted", "Remove"])]
    if d.empty:
        return pd.DataFrame(columns=["provider", "account", "status", "avg3", "last12"])
    last = df["date"].max()
    a3 = d[d["date"] > last - pd.DateOffset(months=3)].groupby(["provider", "account", "status"]).cost_net.sum() / 3
    l12 = d[d["date"] > last - pd.DateOffset(months=12)].groupby(["provider", "account", "status"]).cost_net.sum()
    out = pd.DataFrame({"avg3": a3, "last12": l12}).fillna(0.0).reset_index()
    return out[out["avg3"] >= min_monthly].sort_values("avg3", ascending=False).reset_index(drop=True)


def credits_watch(df: pd.DataFrame) -> pd.DataFrame:
    """Credits per provider per month (a positive number = money taken off the bill) and as a share of gross cost."""
    d = df[df["credits"] != 0]
    if d.empty:
        return pd.DataFrame(columns=["date", "provider", "credits", "gross", "share"])
    cr = d.groupby(["date", "provider"]).credits.sum().mul(-1).rename("credits")
    gross = df[df.provider.isin(d.provider.unique())].groupby(["date", "provider"]).cost_gross.sum().rename("gross")
    t = pd.concat([cr, gross], axis=1).dropna(subset=["credits"]).reset_index()
    t["share"] = t["credits"] / t["gross"].where(t["gross"] > 0)
    return t


def credits_lines(t: pd.DataFrame) -> list[str]:
    out = []
    for p, g in t.groupby("provider"):
        g = g.sort_values("date")
        last, avg6, avg_prev = g["credits"].iloc[-1], g["credits"].iloc[-7:-1].mean() if len(g) > 1 else g["credits"].iloc[-1], g["credits"].iloc[-13:-7].mean() if len(g) > 7 else None
        s = (f"**{p}** credits took {money(last)} off the {g['date'].iloc[-1]:%b %Y} bill ({g['share'].iloc[-1]:.1%} of gross cost), against a {money(avg6)}/month average over the 6 months before")
        if avg_prev and avg_prev > 0:
            s += f" and {money(avg_prev)} the 6 before that"
        if last < 0.8 * avg6:
            s += ". **They are shrinking**: ask whether a credit program is ending"
        elif last > 1.1 * avg6:
            s += ". They are above the recent average, which lowers the net bill and hides part of the gross growth"
        out.append(s + ". The billing data does not carry an end date: check the expiry with the provider.")
    return out


def fig_credits(t: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for p, g in t.groupby("provider"):
        g = g.sort_values("date").tail(18)
        fig.add_bar(x=g["date"], y=g["credits"], name=p, marker_color=COLORS.get(p, "#64748b"), hovertemplate="%{y:$,.0f}")
    fig.update_layout(template=TEMPLATE, height=280, margin=dict(t=20, b=10), barmode="group", legend=dict(orientation="h", y=-0.2), yaxis=dict(tickformat="$.2~s", title="Credits / month"),
                      xaxis=dict(tickformat="%b %y"))
    return fig
