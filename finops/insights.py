from __future__ import annotations
import pandas as pd


def top_movers(df: pd.DataFrame, level: str, month: pd.Timestamp, n: int = 10) -> pd.DataFrame:
    """Biggest $ change vs previous month at `level` (service / account)."""
    prev = month - pd.DateOffset(months=1)
    g = df[df["date"].isin([month, prev])].pivot_table(
        index=level, columns="date", values="cost_net", aggfunc="sum", fill_value=0.0)
    for c in (prev, month):
        if c not in g:
            g[c] = 0.0
    g["change"] = g[month] - g[prev]
    g["change_pct"] = g["change"] / g[prev].where(g[prev] > 0)
    g = g.rename(columns={prev: "previous", month: "current"})
    return g.reindex(g["change"].abs().sort_values(ascending=False).index).head(n).reset_index()


def anomalies(monthly_total: pd.Series, window: int = 6, z: float = 2.0) -> pd.DataFrame:
    """Months deviating > z std-devs from the trailing-window mean."""
    s = monthly_total.sort_index()
    mean = s.rolling(window).mean().shift(1)
    std = s.rolling(window).std().shift(1)
    out = pd.DataFrame({"cost": s, "trailing_mean": mean, "z": (s - mean) / std})
    return out[out["z"].abs() >= z].reset_index()


# ----------------------------------------------------------------------------------------------
# Detectors (rule-based and explainable; "spend at stake" is the amount in question, NOT a savings claim)
# ----------------------------------------------------------------------------------------------
def _pivot(df: pd.DataFrame, level: str) -> pd.DataFrame:
    """A project code is one line across both clouds (provider shows as 'All clouds')."""
    from .savings import ALL, CROSS
    if level in CROSS:
        df = df.assign(provider=ALL)
    return df.pivot_table(index=["provider", level], columns="date", values="cost_net", aggfunc="sum", fill_value=0.0)


def add_notes(t: pd.DataFrame, df: pd.DataFrame, level: str) -> pd.DataFrame:
    """A 'What it is' column from the finance notes of the owner files (account lens and project-code lens)."""
    if t.empty or level not in ("account", "project_code") or "finance_notes" not in df:
        return t
    from . import dims as D
    cur = df[df["date"] > df["date"].max() - pd.DateOffset(months=12)]
    out = t.copy()
    out["What it is"] = out[level].map(D._notes(cur, level)).fillna("")
    return out


def _window(p: pd.DataFrame, month: pd.Timestamp, start_back: int, end_back: int) -> pd.DataFrame:
    """Columns from month-start_back .. month-end_back (inclusive), reindexed so missing months count as 0."""
    idx = [month - pd.DateOffset(months=i) for i in range(start_back, end_back - 1, -1)]
    return p.reindex(columns=idx, fill_value=0.0)


def spikes(df: pd.DataFrame, level: str, month: pd.Timestamp, min_delta: float = 5000, min_pct: float = 0.3) -> pd.DataFrame:
    """Entities whose month is well above their own trailing 3-month average (they already had spend)."""
    p = _pivot(df, level)
    base = _window(p, month, 3, 1).mean(axis=1)
    cur = p.reindex(columns=[month], fill_value=0.0)[month]
    out = pd.DataFrame({"trailing_3mo_avg": base, "this_month": cur})
    out["increase"] = out["this_month"] - out["trailing_3mo_avg"]
    out["increase_pct"] = out["increase"] / out["trailing_3mo_avg"].where(out["trailing_3mo_avg"] > 100)
    out = out[(out["trailing_3mo_avg"] > 100) & (out["increase"] >= min_delta) & (out["increase_pct"] >= min_pct)]
    return out.sort_values("increase", ascending=False).reset_index()


def new_spend(df: pd.DataFrame, level: str, month: pd.Timestamp, min_amount: float = 5000) -> pd.DataFrame:
    """Entities with (almost) no spend in the prior 3 months that are now material."""
    p = _pivot(df, level)
    prior = _window(p, month, 3, 1)
    cur = p.reindex(columns=[month], fill_value=0.0)[month]
    out = pd.DataFrame({"prior_3mo_max": prior.max(axis=1), "this_month": cur})
    out = out[(out["prior_3mo_max"] < 100) & (out["this_month"] >= min_amount)]
    return out.sort_values("this_month", ascending=False).reset_index()


def sustained_growth(df: pd.DataFrame, level: str, month: pd.Timestamp, min_delta: float = 3000, min_pct: float = 0.25) -> pd.DataFrame:
    """Last-3-month average vs the 3 months before it: the quiet creep spikes don't catch."""
    p = _pivot(df, level)
    last3 = _window(p, month, 2, 0).mean(axis=1)
    prior3 = _window(p, month, 5, 3).mean(axis=1)
    out = pd.DataFrame({"prior_3mo_avg": prior3, "last_3mo_avg": last3})
    out["increase_per_month"] = out["last_3mo_avg"] - out["prior_3mo_avg"]
    out["increase_pct"] = out["increase_per_month"] / out["prior_3mo_avg"].where(out["prior_3mo_avg"] > 1000)
    out["annualized"] = out["increase_per_month"] * 12
    out = out[(out["prior_3mo_avg"] > 1000) & (out["increase_per_month"] >= min_delta) & (out["increase_pct"] >= min_pct)]
    return out.sort_values("increase_per_month", ascending=False).reset_index()


def ownership_gap(df: pd.DataFrame, month: pd.Timestamp) -> pd.DataFrame:
    """Accounts/projects with no owner in the owner CSVs."""
    d = df[df["owner"] == "(unassigned)"]
    last12 = d[d["date"] > month - pd.DateOffset(months=12)].groupby(["provider", "account"])["cost_net"].sum()
    cur = d[d["date"] == month].groupby(["provider", "account"])["cost_net"].sum()
    out = pd.DataFrame({"this_month": cur, "last_12mo": last12}).fillna(0.0)
    return out[out["last_12mo"] > 0].sort_values("last_12mo", ascending=False).reset_index()


def structure_flags(df: pd.DataFrame, month: pd.Timestamp) -> pd.DataFrame:
    """Cost-structure checks. severity: High / Medium / Info."""
    flags = []
    cur = df[df["date"] == month]
    l12 = df[df["date"] > month - pd.DateOffset(months=12)]

    def add(sev, finding, evidence, amount):
        flags.append({"severity": sev, "finding": finding, "evidence": evidence, "spend_at_stake": amount})

    aws, gcp = cur[cur.provider == "AWS"], cur[cur.provider == "GCP"]
    if len(aws) and aws.cost_net.sum() > 0:
        tot = aws.cost_net.sum()
        sup = aws[aws.service.str.contains("Support", case=False)].cost_net.sum()
        if sup / tot > 0.05:
            add("Info", "AWS Support is a large share of AWS Cloud Services",
                f"{sup / tot:.0%} of AWS spend. Enterprise Support is a % of spend — review the tier vs. the value used.", sup)
        ec2 = aws[aws.service == "Amazon Elastic Compute Cloud - Compute"].cost_net.sum()
        other = aws[aws.service == "EC2 - Other"].cost_net.sum()
        if ec2 > 0 and other / ec2 > 0.25:
            add("Medium", "EC2-Other (EBS, NAT gateways, data transfer) is large relative to EC2 compute",
                f"EC2-Other is {other / ec2:.0%} of EC2 compute. Look for idle EBS volumes/snapshots, NAT and cross-AZ transfer.", other)
        sp = aws[aws.service.str.contains("Savings Plans", case=False)].cost_net.sum()
        if sp > 0:
            add("Info", "Savings Plans fee present",
                f"{money_(sp)} this month. Coverage/utilization is not in this data — check Cost Explorer's Savings Plans reports.", sp)
    if len(gcp) and gcp.cost_net.sum() > 0:
        tot = gcp.cost_net.sum()
        nop = gcp[gcp.account == "(no project)"].cost_net.sum()
        if nop / tot > 0.10:
            add("Medium", "GCP spend not attached to any project",
                f"{nop / tot:.0%} of GCP spend is '(no project)' (support, invoice-level items, third-party). Hard to allocate.", nop)
        untag = gcp[gcp.env == "untagged"].cost_net.sum()
        if untag / tot > 0.30:
            add("Medium", "Most GCP spend has no env/environment label",
                f"{untag / tot:.0%} of GCP spend is unlabeled — limits prod vs non-prod analysis.", untag)
    tot_all = cur.cost_net.sum()
    un = cur[cur.owner == "(unassigned)"].cost_net.sum()
    if tot_all > 0 and un / tot_all > 0.10:
        add("High" if un / tot_all > 0.25 else "Medium", "Spend with no owner",
            f"{un / tot_all:.0%} of this month's spend sits in accounts/projects with no owner in the owner files.", un)
    # concentration
    if tot_all > 0:
        top = cur.groupby(["provider", "service"]).cost_net.sum().nlargest(3)
        share = top.sum() / tot_all
        if share > 0.5:
            add("Info", "Spend is concentrated in a few services",
                f"Top 3 services = {share:.0%}: " + "; ".join(f"{p} {s[:28]}" for (p, s) in top.index), top.sum())
    out = pd.DataFrame(flags, columns=["severity", "finding", "evidence", "spend_at_stake"])
    order = {"High": 0, "Medium": 1, "Info": 2}
    return out.sort_values(["severity", "spend_at_stake"], key=lambda s: s.map(order) if s.name == "severity" else -s).reset_index(drop=True)


def money_(v: float) -> str:
    return f"${v / 1e6:,.2f}M" if abs(v) >= 1e6 else f"${v / 1e3:,.0f}K" if abs(v) >= 1e4 else f"${v:,.0f}"
