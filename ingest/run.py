"""Refresh the dashboard's data with YOUR credentials.

    python -m ingest.run --provider aws [--profile NAME]     # Cost Explorer (SSO)
    python -m ingest.run --provider gcp                      # BigQuery (gcloud ADC)
    python -m ingest.run --provider all
    python -m ingest.run --provider aws --tags-only          # only the tag / label tables
"""
from __future__ import annotations
import argparse
import sys

from finops import store
from finops.config import load_config


def _save_tags(provider: str, tables) -> None:
    from finops import tags
    acc, vals, cat = tables
    tags.save_provider(provider, acc, vals, cat)
    got = cat[cat.pulled]
    print(f"{provider} tags: {len(acc)} accounts/projects, {len(got)} of {len(cat)} tag keys pulled ({len(vals):,} account x value rows)")


def _add_new(provider: str) -> None:
    """New accounts / projects found in billing go into the owner file with status New, so you can fill their fields."""
    from finops import status
    try:
        added = status.add_new(provider)
    except Exception as e:                                       # never fail a billing refresh over the owner file
        print(f"{provider} new accounts not added: {type(e).__name__}: {str(e)[:200]}")
        return
    if added:
        print(f"{provider}: {len(added)} new account(s) added to the owner file as New: " + ", ".join(added[:8]) + ("…" if len(added) > 8 else ""))


def run_aws(months: int, profile: str | None, billing: bool = True, tags: bool = True) -> None:
    from ingest import aws
    session = aws.user_session(profile)
    if billing:
        df = aws.fetch(session, months)
        store.upsert(df, "AWS", note=f"Cost Explorer, {months} months")
        print(f"AWS: {len(df):,} rows, {df.date.min():%Y-%m} -> {df.date.max():%Y-%m}, net ${df.cost_net.sum():,.0f}")
        _add_new("AWS")
    if tags:
        try:
            _save_tags("AWS", aws.fetch_tags(session))
        except Exception as e:                                   # tags are extra: never fail the billing refresh over them
            print(f"AWS tags skipped: {type(e).__name__}: {str(e)[:300]}")
            if not billing:
                raise


def run_gcp(months: int, billing: bool = True, tags: bool = True) -> None:
    from ingest import gcp
    problem = gcp.check_credentials()
    if problem:
        sys.exit(problem)
    if billing:
        df = gcp.fetch(months)
        store.upsert(df, "GCP", note=f"BigQuery export, {months} months")
        print(f"GCP: {len(df):,} rows, {df.date.min():%Y-%m} -> {df.date.max():%Y-%m}, net ${df.cost_net.sum():,.0f}")
        _add_new("GCP")
    if tags:
        try:
            _save_tags("GCP", gcp.fetch_tags())
        except Exception as e:
            print(f"GCP tags skipped: {type(e).__name__}: {str(e)[:300]}")
            if not billing:
                raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["aws", "gcp", "all"], default="all")
    ap.add_argument("--months", type=int, default=load_config()["data"]["history_months"])
    ap.add_argument("--profile", help="AWS profile (default: SSO device login)")
    ap.add_argument("--tags-only", action="store_true", help="refresh only the tag / label tables (no billing pull)")
    ap.add_argument("--no-tags", action="store_true", help="skip the tag / label tables")
    a = ap.parse_args()
    if a.provider in ("aws", "all"):
        run_aws(a.months, a.profile, billing=not a.tags_only, tags=not a.no_tags)
    if a.provider in ("gcp", "all"):
        run_gcp(a.months, billing=not a.tags_only, tags=not a.no_tags)
    print("Done.")


if __name__ == "__main__":
    main()
