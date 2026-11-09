"""Normalized cost schema shared by every provider loader.

One row = one month x provider x account/project x service x env.
All dashboard pages read ONLY this shape, so adding a source = adding a loader.
"""
from __future__ import annotations
import pandas as pd

CLOUD, MARKETPLACE, TAX = "Cloud Services", "Marketplace", "Tax"
CATEGORIES = [CLOUD, MARKETPLACE, TAX]

COLUMNS = [
    "date",         # first day of the (invoice) month, datetime64
    "provider",     # "AWS" | "GCP"
    "account",      # AWS account / GCP project
    "service",
    "category",     # "Cloud Services" (budget basis) | "Marketplace" | "Tax"  -- always kept separate
    "env",          # prod / nonprod / dev / untagged / unknown
    "cost_gross",   # before credits
    "credits",      # negative number = reduces cost
    "cost_net",     # gross + credits  (the number executives care about)
    "owner",        # from the owners CSVs ("(unassigned)" if unknown)
    "family",       # application family (account/project grouping)
    "is_partial",   # True for "as now" (month still in progress); billed = False
]


def finalize(df: pd.DataFrame) -> pd.DataFrame:
    defaults = {"category": CLOUD, "owner": "(unassigned)", "family": "(unassigned)", "is_partial": False}
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = defaults.get(c, 0.0 if c.startswith("cost") or c == "credits" else "unknown")
    df["date"] = pd.to_datetime(df["date"]).dt.to_period("M").dt.to_timestamp()
    return df[COLUMNS].reset_index(drop=True)
