"""Storage and credentials for the platforms that are NOT AWS / GCP (Equinix, Grafana).

They never touch billing.parquet, the budgets, the savings ledger or the project-code lens: each platform keeps its own small
Parquet files in the data folder, and its own refresh log. Credentials are read from an env file (git-ignored) or the process
environment, held in memory only, and never written anywhere by the dashboard."""
from __future__ import annotations
import getpass
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import ROOT, load_config, resolve

LOG_COLS = ["when", "who", "platform", "first_month", "last_month", "rows", "total", "note"]


def data_dir() -> Path:
    d = resolve(load_config()["data"]["dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def section(name: str) -> dict:
    """The [name] table of config.toml ({} when absent)."""
    return dict(load_config().get(name, {}))


# ---------------------------------------------------------------------------------------------- tables
def read(name: str, columns: list[str]) -> pd.DataFrame:
    p = data_dir() / f"{name}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame({c: [] for c in columns})


def merge_months(name: str, new: pd.DataFrame, columns: list[str], month_col: str = "month") -> pd.DataFrame:
    """Replace every month present in `new`, keep older months already saved (the APIs only keep a limited window, so history accumulates)."""
    cur = read(name, columns)
    keep = cur[~cur[month_col].isin(new[month_col].unique())] if len(cur) else cur
    out = pd.concat([keep, new], ignore_index=True).sort_values(month_col).reset_index(drop=True)
    out.to_parquet(data_dir() / f"{name}.parquet", index=False)
    return out


# ---------------------------------------------------------------------------------------------- refresh log
def log_refresh(platform: str, first: pd.Timestamp, last: pd.Timestamp, rows: int, total: float, note: str = "") -> None:
    row = {"when": datetime.now().isoformat(timespec="seconds"), "who": getpass.getuser(), "platform": platform,
           "first_month": f"{first:%Y-%m}", "last_month": f"{last:%Y-%m}", "rows": rows, "total": round(total, 2), "note": note}
    pd.concat([read_log(), pd.DataFrame([row])], ignore_index=True).to_csv(data_dir() / "platforms_log.csv", index=False)


def read_log(platform: str | None = None) -> pd.DataFrame:
    p = data_dir() / "platforms_log.csv"
    log = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=LOG_COLS)
    return log[log["platform"].eq(platform)] if platform else log


def last_refresh(platform: str) -> str:
    log = read_log(platform)
    return str(log["when"].max()) if len(log) else "never"


# ---------------------------------------------------------------------------------------------- credentials
def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value lines. Values stay in memory; nothing is exported to os.environ."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def credentials(cfg_name: str, keys: list[str]) -> tuple[dict[str, str], str]:
    """(values, where): the process environment wins, then the env file named in config.toml [cfg_name] env_file (default: .env next to the app)."""
    file = section(cfg_name).get("env_file", ".env")
    path = Path(file).expanduser()
    path = path if path.is_absolute() else ROOT / path
    from_file = load_env_file(path)
    vals = {k: os.environ.get(k) or from_file.get(k, "") for k in keys}
    return vals, str(path)


def redact(text: str, secrets: list[str]) -> str:
    """Remove every secret from a string before it is logged or raised."""
    for s in secrets:
        if s and len(s) > 6:
            text = text.replace(s, "<redacted>")
    return text
