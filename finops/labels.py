from __future__ import annotations
import pandas as pd
from .ui import current_view


def relabel(df: pd.DataFrame) -> pd.DataFrame:
    """Friendlier display names. GCP '(no project)' is the Support contract in the Cloud Services view."""
    if df.empty:
        return df
    nm = "Google Support (no project)" if current_view().startswith("Cloud") else "(no project): third-party / invoice"
    m = df["provider"].eq("GCP") & df["account"].eq("(no project)")
    return df.assign(account=df["account"].where(~m, nm))
