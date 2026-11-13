"""Deterministic synthetic billing data with every situation the dashboard has to handle. No real data anywhere.

AWS  Acct-Steady   flat $50K EC2 + a Savings Plans fee that has gaps
     Acct-Shrunk   RDS $40K -> $20K (a real saving, month 12)
     Acct-Ended    S3 $15K for 14 months, then $0 (a shutdown)
     Acct-Spike    EC2 $10K with one $200K month (a one-off)
GCP  prj-ramp      BigQuery ramping $5K -> ~$60K (growth)
     prj-small     $800 flat (immaterial)
Marketplace: AWS Datadog monthly $20K, AWS CrowdStrike annual $60K, GCP Vendor Q quarterly $30K.  Tax: 3% of AWS cloud.
Plus an in-progress (partial) month after the last billed one."""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd

END = pd.Timestamp("2026-10-01")
OWNERS = {"Acct-Steady": "Alice", "Acct-Shrunk": "Alice", "Acct-Ended": "Bob", "Acct-Spike": "Bob", "prj-ramp": "Cara", "prj-small": "(unassigned)"}


def months(n: int = 27, end: pd.Timestamp = END) -> pd.DatetimeIndex:
    return pd.date_range(end=end, periods=n, freq="MS")


def make_billing(n_months: int = 27, partial: bool = True) -> pd.DataFrame:
    idx = months(n_months)
    rows = []

    def add(provider, account, service, values, category="Cloud Services"):
        for d, v in zip(idx, values):
            rows.append(dict(date=d, provider=provider, account=account, service=service, category=category, env="unknown",
                             cost_gross=v, credits=0.0, cost_net=v, owner=OWNERS.get(account, "(unassigned)"), family=account, is_partial=False))

    n = len(idx)
    add("AWS", "Acct-Steady", "Amazon Elastic Compute Cloud - Compute", [50_000.0] * n)
    sp = np.full(n, 5_000.0)
    sp[[i for i in (8, 9, 20) if i < n]] = 0.0                                          # billing gaps
    add("AWS", "Acct-Steady", "Savings Plans for AWS Compute usage", sp)
    add("AWS", "Acct-Shrunk", "Amazon Relational Database Service", [40_000.0] * max(n - 15, 0) + [20_000.0] * min(15, n))
    add("AWS", "Acct-Ended", "Amazon Simple Storage Service", [15_000.0] * min(14, n) + [0.0] * max(n - 14, 0))
    spike = np.full(n, 10_000.0)
    spike[min(6, n - 1)] = 200_000.0
    add("AWS", "Acct-Spike", "Amazon Elastic Compute Cloud - Compute", spike)
    add("GCP", "prj-ramp", "BigQuery", np.linspace(5_000, 60_000, n))
    add("GCP", "prj-small", "Cloud Storage", [800.0] * n)
    add("AWS", "Acct-Steady", "Datadog Pro", [20_000.0] * n, "Marketplace")
    add("AWS", "Acct-Steady", "CrowdStrike Falcon", [60_000.0 if d.month == 3 else 0.0 for d in idx], "Marketplace")
    add("GCP", "prj-ramp", "Vendor Q (reseller)", [30_000.0 if d.month in (1, 4, 7, 10) else 0.0 for d in idx], "Marketplace")
    add("AWS", "Acct-Steady", "Tax", [3_000.0] * n, "Tax")
    df = pd.DataFrame(rows)
    if partial:
        p = df[df.date == idx[-1]].copy()
        p["date"] = idx[-1] + pd.DateOffset(months=1)
        p[["cost_gross", "cost_net"]] = p[["cost_gross", "cost_net"]] * 0.3
        p["is_partial"] = True
        df = pd.concat([df, p], ignore_index=True)
    return df


def make_equinix_raw(n: int = 12, end: pd.Timestamp = END + pd.DateOffset(months=1)):
    """Fake Equinix Billing API payloads: (invoices, lines, names). Last month: Node1 ends, a new Internet line appears, the router goes up.
    One $5,000 credit in the 6th month."""
    idx = pd.date_range(end=end, periods=n, freq="MS")
    uid = lambda k: f"{k:08d}-0000-0000-0000-000000000000"
    names = {uid(1): "Lab-FW-Node0", uid(2): "Lab-FW-Node1", uid(3): "lab-router", uid(4): "lab-aws-primary"}
    invoices, lines = [], []
    for i, d in enumerate(idx):
        last = i == n - 1
        defs = [("NEDG-1", "Network Edge firewall", uid(1), 2200.0), ("AWS-1", "Fabric to AWS virtual connection", uid(4), 500.0),
                ("CR-1", "Cloud Router", uid(3), 1400.0 if last else 1200.0)]
        if not last:
            defs.append(("NEDG-2", "Network Edge firewall", uid(2), 2200.0))
        else:
            defs.append(("EC0-1", "Internet Access 1G", None, 160.0))
        inv_id = f"INV{i:03d}"
        for code, desc, u, amt in defs:
            lines.append(dict(transactionDate=f"{d:%Y-%m-%d}", transactionId=inv_id, activityType="RECURRING", productCode=code, productName=desc,
                              productDescription=f"{desc} {u or ''} -Recurring Charge", recurringAmount=amt, nonRecurringAmount=0, adjustment=0,
                              taxAmount=amt * 0.05, currencyCode="USD"))
        total = sum(x[3] for x in defs)
        invoices.append(dict(transactionDate=f"{d:%Y-%m-%d}", transactionId=inv_id, transactionType="INVOICE", currencyCode="USD",
                             totalRecurringAmount=total, totalNonRecurringAmount=0, totalAmount=total * 1.05))
        if i == 5:
            lines.append(dict(transactionDate=f"{d:%Y-%m-%d}", transactionId="CM001", activityType="CREDIT", productCode="NEDG-1", productName="Credit",
                              productDescription="Network Edge firewall credit", recurringAmount=-5000.0, nonRecurringAmount=0, adjustment=0, taxAmount=0, currencyCode="USD"))
            invoices.append(dict(transactionDate=f"{d:%Y-%m-%d}", transactionId="CM001", transactionType="CREDIT_MEMO", currencyCode="USD",
                                 totalRecurringAmount=-5000.0, totalNonRecurringAmount=0, totalAmount=-5000.0))
    return invoices, lines, names


def write_config(path: Path, data_dir: Path) -> Path:
    path.write_text(f"""
[fiscal]
start_month = 1
[budget.2026]
aws_monthly = 100000
gcp_monthly = 50000
[budget.2027]
aws_monthly = 0
gcp_monthly = 0
[forecast]
yoy_window_months = 6
default_growth_pct = 0.0
default_band_pct = 15
adjustments_csv = "{data_dir / 'anomaly_rules.csv'}"
[forecast.trend_since]
[data]
dir = "{data_dir}"
history_months = 27
[aws]
sso_start_url = "https://example.invalid/start"
sso_region = "us-east-1"
rollup_account_id = "111111111111"
[gcp]
project = "example"
table = "example.dataset.table"
[owners]
aws_csv = "{data_dir / 'owners_aws.csv'}"
gcp_csv = "{data_dir / 'owners_gcp.csv'}"
[audit]
dir = "{data_dir / 'audits'}"
gcp_names = "{data_dir / 'orphans_gcp.txt'}"
idle_below_usd = 1
hide_notes = []
""", encoding="utf-8")
    return path


def make_env(root: Path, with_data: bool = True, n_months: int = 27) -> Path:
    """A throwaway data folder + config.toml; returns the config path (point FINOPS_CONFIG at it)."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    if with_data:
        make_billing(n_months).to_parquet(data / "billing.parquet", index=False)
        from finops import equinix as eq
        ln, inv = eq.normalize(*make_equinix_raw())
        ln.to_parquet(data / "equinix_lines.parquet", index=False)
        inv.to_parquet(data / "equinix_invoices.parquet", index=False)
    return write_config(root / "config.toml", data)
