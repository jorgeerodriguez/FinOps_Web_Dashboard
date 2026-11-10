"""Marketplace / third-party contract calendar.

Ported from your cloud_forecast.contract_schedule / _marketplace: each contract's billing cadence is read from
its last 12 billed months (compared with the 12 before, when loaded) and repeated forward (monthly = avg of last 3; regular gap = last amount at that gap;
single charge = annual renewal, same month next year). Marketplace is lumpy, so a statistical forecast is the
wrong tool -- the renewal calendar is the budget.
"""
from __future__ import annotations
import re
import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

MIN_CHARGE = 100          # a month counts as "billed" above this amount
BASIS_MONTHS = 12

VENDOR_PATTERNS = [
    (r"Confluent", "Confluent (Kafka/Flink)"), (r"Datadog", "Datadog"), (r"CrowdStrike", "CrowdStrike"),
    (r"Grafana Cloud", "Grafana Cloud"), (r"Kong", "Kong Konnect"), (r"WIZ", "Wiz"), (r"Red Canary", "Red Canary"),
    (r"Upwind", "Upwind"), (r"Normalyze", "Normalyze"), (r"NetSPI", "NetSPI"), (r"Proofpoint", "Proofpoint"),
    (r"Tines", "Tines"), (r"dbt Cloud", "dbt Cloud"), (r"CloudFix", "CloudFix"), (r"Conduktor", "Conduktor"),
    (r"Palo Alto", "Palo Alto Networks"), (r"GitLab", "GitLab"), (r"Aviatrix", "Aviatrix"),
    (r"SFTP Gateway", "SFTP Gateway"), (r"Workday", "Workday"), (r"ZoomInfo", "ZoomInfo"),
    (r"Amazon Bedrock Edition|Claude Platform", "Anthropic Claude (Bedrock)"),
]


def contract_key(service: str) -> str:
    """Fold every Claude model / platform line into one usage-based contract."""
    if "Amazon Bedrock Edition" in service or service == "Claude Platform":
        return "Anthropic Claude (Bedrock usage)"
    return service


def vendor_label(service: str) -> str:
    for pat, label in VENDOR_PATTERNS:
        if re.search(pat, service):
            return label
    return service if len(service) <= 40 else service[:38] + "…"


def contract_schedule(s: pd.Series, horizon: pd.DatetimeIndex):
    """(cadence, amount per charge, schedule over `horizon`) from the basis-window series, or None."""
    billed = s[s > MIN_CHARGE]
    n = len(billed)
    if n == 0:
        return None
    sched = pd.Series(0.0, index=horizon)
    if n >= 5 or (s.tail(3) > MIN_CHARGE).all():
        cadence, amount = "Monthly", s.tail(3).mean()
        sched[:] = amount
    else:
        billed = billed[billed >= 0.10 * billed.max()]          # ignore small true-ups/noise when reading the cadence
        n = len(billed)
        if n >= 2:
            gaps = np.diff([d.year * 12 + d.month for d in billed.index])
            k = max(int(round(np.median(gaps))), 1)
            cadence = {1: "Monthly", 2: "Bimonthly", 3: "Quarterly", 4: "Every 4 months", 6: "Semiannual"}.get(k, f"Every {k} months")
            amount = billed.iloc[-1]
            d = billed.index[-1] + relativedelta(months=k)
            while d <= horizon[-1]:
                if d in sched.index:
                    sched[d] = amount
                d += relativedelta(months=k)
        else:
            cadence, amount = "Annual", billed.iloc[-1]
            d = billed.index[-1] + relativedelta(years=1)
            if d in sched.index:
                sched[d] = amount
    return cadence, amount, sched


def build(mkt: pd.DataFrame, horizon_end: pd.Timestamp):
    """mkt: billed Marketplace rows (schema). Returns (contracts table, monthly matrix).

    matrix: DataFrame indexed by (provider, contract, vendor) x month; plus `kind` per column in matrix.attrs.
    """
    mkt = mkt.assign(contract=mkt["service"].map(contract_key))
    last = mkt["date"].max()
    all_months = pd.date_range(mkt["date"].min(), last, freq="MS")
    future = pd.date_range(last + relativedelta(months=1), horizon_end, freq="MS")
    basis_idx = all_months[-BASIS_MONTHS:]
    piv = (mkt.pivot_table(index="date", columns=["provider", "contract"], values="cost_net", aggfunc="sum")
           .reindex(all_months).fillna(0.0))
    cols, rows = {}, []
    for (p, c), s in piv.items():
        res = contract_schedule(s.reindex(basis_idx), future)
        if res is None:                                    # nothing billed in the last 12 months
            cols[(p, c)] = pd.concat([s, pd.Series(0.0, index=future)])
            rows.append(dict(provider=p, contract=c, vendor=vendor_label(c), cadence="Not billed in last 12 mo",
                             amount=0.0, next_charge=pd.NaT))
            continue
        cadence, amount, sched = res
        cols[(p, c)] = pd.concat([s, sched])
        nxt = sched[sched > 0]
        rows.append(dict(provider=p, contract=c, vendor=vendor_label(c), cadence=cadence, amount=amount,
                         next_charge=nxt.index[0] if len(nxt) else pd.NaT))
    m = pd.DataFrame(cols)
    m.index.name = "date"
    ct = pd.DataFrame(rows).set_index(["provider", "contract"])
    ct["billed_12mo"] = piv.reindex(basis_idx).sum()
    prev_idx = pd.date_range(basis_idx[0] - relativedelta(months=BASIS_MONTHS), basis_idx[0] - relativedelta(months=1), freq="MS")
    have_prev = prev_idx[0] >= all_months[0]                     # only meaningful when the 12 months before the basis are loaded
    ct["billed_prev12"] = piv.reindex(prev_idx).sum() if have_prev else np.nan
    return ct.reset_index(), m, last


def stale_charges(mkt: pd.DataFrame, ct: pd.DataFrame, min_amount: float = 10_000) -> pd.DataFrame:
    """Contracts billed earlier in the window but not in the last 12 months -> ended or renewal to confirm."""
    dead = ct[ct["cadence"] == "Not billed in last 12 mo"]
    if dead.empty:
        return dead
    mm = mkt.assign(contract=mkt["service"].map(contract_key))
    g = mm.groupby(["provider", "contract"]).agg(amount=("cost_net", "sum"), last_billed=("date", "max")).reset_index()
    out = dead[["provider", "contract", "vendor"]].merge(g, on=["provider", "contract"])
    return out[out["amount"] >= min_amount].sort_values("amount", ascending=False)
