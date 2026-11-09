from __future__ import annotations
import pandas as pd
from . import store
from .owners import attach_owners


def load_all(include_partial: bool = False) -> pd.DataFrame:
    """Billed data by default; include_partial=True adds the in-progress month ('as now')."""
    df = store.read()
    if df.empty:
        return df
    df = attach_owners(df)              # owners applied at read time so edits to the CSVs show on Reload
    return df if include_partial else df[~df["is_partial"].astype(bool)]


def monthly(df: pd.DataFrame, by: list[str] | None = None, col: str = "cost_net") -> pd.DataFrame:
    return df.groupby(["date"] + (by or []), as_index=False)[col].sum()
