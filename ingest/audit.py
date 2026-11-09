"""Run an orphan audit with YOUR credentials and save the result to data/audits/.

    python -m ingest.audit --provider aws [--profile NAME]
    python -m ingest.audit --provider gcp [--names data/orphans_gcp.txt]     # ~15-20 min for ~30 projects
"""
from __future__ import annotations
import argparse
import sys

from finops import audit as audit_store
from finops import store
from finops.config import load_config, resolve


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["aws", "gcp"], required=True)
    ap.add_argument("--names", help="GCP: file with project names/IDs, one per line")
    ap.add_argument("--profile", help="AWS profile (default: SSO device login)")
    a = ap.parse_args()
    p = lambda m: print(m, flush=True)
    if a.provider == "gcp":
        from ingest import gcp as gcp_ingest, gcp_audit
        problem = gcp_ingest.check_credentials()
        if problem:
            sys.exit(problem)
        names_file = a.names or str(resolve(load_config()["audit"]["gcp_names"]))
        names = gcp_audit.read_names(names_file)
        p(f"Auditing {len(names)} names from {names_file}")
        rows = gcp_audit.run(names, p)
    else:
        from ingest import aws_audit
        from finops.data import load_all
        cost = load_all(include_partial=False)
        if cost.empty:
            sys.exit("No billing data yet: refresh AWS data first (Data Health page).")
        rows = aws_audit.run(cost, a.profile, p)
    path = audit_store.save_audit(a.provider.upper(), rows)
    p(f"Saved {len(rows)} rows -> {path}")
    p("Done.")


if __name__ == "__main__":
    main()
