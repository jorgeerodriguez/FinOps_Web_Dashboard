"""The dashboard's own data store: one Parquet file in the normalized schema + a refresh log."""
from __future__ import annotations
import getpass
from datetime import datetime
import pandas as pd
from .config import load_config, resolve
from .schema import COLUMNS, finalize

LOG_COLS = ["when", "who", "provider", "first_month", "last_month", "rows", "total_net", "note"]


def _dir():
    d = resolve(load_config()["data"]["dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def read() -> pd.DataFrame:
    p = _dir() / "billing.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame({c: [] for c in COLUMNS})


def upsert(new: pd.DataFrame, provider: str, note: str = "") -> None:
    """Replace this provider's rows for every month present in `new`; keep everything else."""
    new = finalize(new)
    cur = read()
    drop = cur["provider"].eq(provider) & cur["date"].isin(new["date"].unique())
    out = pd.concat([cur[~drop], new], ignore_index=True).sort_values(["provider", "date"])
    out.to_parquet(_dir() / "billing.parquet", index=False)
    log = read_log()
    row = {"when": datetime.now().isoformat(timespec="seconds"), "who": getpass.getuser(), "provider": provider,
           "first_month": f"{new['date'].min():%Y-%m}", "last_month": f"{new['date'].max():%Y-%m}",
           "rows": len(new), "total_net": round(new["cost_net"].sum(), 2), "note": note}
    pd.concat([log, pd.DataFrame([row])], ignore_index=True).to_csv(_dir() / "refresh_log.csv", index=False)


def read_log() -> pd.DataFrame:
    p = _dir() / "refresh_log.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=LOG_COLS)
