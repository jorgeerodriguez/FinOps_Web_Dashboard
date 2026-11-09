"""Refresh the dashboard's data with YOUR credentials.

    python -m ingest.run --provider aws [--profile NAME]     # Cost Explorer (SSO)
    python -m ingest.run --provider gcp                      # BigQuery (gcloud ADC)
    python -m ingest.run --provider all
"""
from __future__ import annotations
import argparse
import sys

from finops import store
from finops.config import load_config


def run_aws(months: int, profile: str | None) -> None:
    from ingest import aws
    df = aws.fetch(aws.user_session(profile), months)
    store.upsert(df, "AWS", note=f"Cost Explorer, {months} months")
    print(f"AWS: {len(df):,} rows, {df.date.min():%Y-%m} -> {df.date.max():%Y-%m}, net ${df.cost_net.sum():,.0f}")


def run_gcp(months: int) -> None:
    from ingest import gcp
    problem = gcp.check_credentials()
    if problem:
        sys.exit(problem)
    df = gcp.fetch(months)
    store.upsert(df, "GCP", note=f"BigQuery export, {months} months")
    print(f"GCP: {len(df):,} rows, {df.date.min():%Y-%m} -> {df.date.max():%Y-%m}, net ${df.cost_net.sum():,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["aws", "gcp", "all"], default="all")
    ap.add_argument("--months", type=int, default=load_config()["data"]["history_months"])
    ap.add_argument("--profile", help="AWS profile (default: SSO device login)")
    a = ap.parse_args()
    if a.provider in ("aws", "all"):
        run_aws(a.months, a.profile)
    if a.provider in ("gcp", "all"):
        run_gcp(a.months)
    print("Done.")


if __name__ == "__main__":
    main()
