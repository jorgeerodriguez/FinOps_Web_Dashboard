"""Attach owner / family from your hand-maintained owner CSVs (blank owner -> '(unassigned)')."""
from __future__ import annotations
import pandas as pd
from .config import load_config, resolve

UNASSIGNED = "(unassigned)"

# Columns of the owner files that are joined onto every billing row at read time (typed by you in Data Health)
DIM_COLS = ["environment", "project_code", "cost_center", "service_name", "finance_notes", "status"]
STATUSES = ["New", "Active", "Idle", "Deleted", "Orphan", "Remove"]       # lifecycle of an account / project (see finops/status.py)
MISSING = {"environment": "unspecified", "project_code": "(no project code)", "cost_center": "(no cost center)",
           "service_name": "(no service name)", "finance_notes": "", "status": "(not set)"}
_ENV_ALIASES = {"nonprod": "non-prod", "non prod": "non-prod", "non_prod": "non-prod", "production": "prod", "prd": "prod"}
PLACEHOLDERS = ["placeholder", "tbd", "n/a", "na", "none", "unknown", "-", "?", "unspecified"]


def _read(key: str) -> pd.DataFrame:
    p = resolve(load_config().get("owners", {}).get(key, ""))
    if not p.is_file():
        return pd.DataFrame()
    d = pd.read_csv(p, dtype=str).fillna("")
    return d.apply(lambda c: c.str.strip())          # stray spaces ("Jane Doe ") must not create a second owner


def _maps() -> dict[str, tuple[dict, dict]]:
    out = {}
    a = _read("aws_csv")
    if len(a):
        a["account_id"] = a["account_id"].str.zfill(12)
        own = {**dict(zip(a["account_id"], a["owner"])), **dict(zip(a["account_name"], a["owner"]))}
        fam = {**dict(zip(a["account_id"], a["family"])), **dict(zip(a["account_name"], a["family"]))}
        out["AWS"] = (own, fam)
    g = _read("gcp_csv")
    if len(g):
        out["GCP"] = (dict(zip(g["project"], g["owner"])), dict(zip(g["project"], g["family"])))
    return out


def is_missing(series: pd.Series, col: str) -> pd.Series:
    return series.eq(MISSING[col]) if MISSING[col] else series.eq("")


def _placeholders() -> set[str]:
    return {x.lower() for x in load_config().get("dimensions", {}).get("placeholders", PLACEHOLDERS)}


def _canon(values: pd.Series, col: str) -> pd.Series:
    """Tidy one column: trim, treat blanks / placeholders as missing, one spelling per case-insensitive value (the most common one wins),
    and a fixed vocabulary for environment."""
    v = values.fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    if col == "finance_notes":
        return v
    low = v.str.lower()
    if col == "status":
        canon = {x.lower(): x for x in STATUSES}
        return low.map(lambda x: canon.get(x, MISSING["status"] if x == "" else v[low == x].iloc[0]))
    if col == "environment":
        return low.map(lambda x: MISSING["environment"] if x in _placeholders() or x == "" else _ENV_ALIASES.get(x, x))
    blank = (low == "") | low.isin(_placeholders())
    spell = v[~blank].groupby(low[~blank]).agg(lambda x: x.value_counts().index[0])          # most common spelling per case-insensitive key
    return v.where(blank, low.map(spell)).where(~blank, MISSING[col])


def _dim_maps() -> dict[str, dict[str, dict]]:
    """{provider: {column: {account name or id: tidy value}}} from both owner files, spelled consistently across providers."""
    files = {"AWS": _read("aws_csv"), "GCP": _read("gcp_csv")}
    out = {p: {} for p in files}
    for col in DIM_COLS:
        parts = {p: (d[col] if col in d and len(d) else pd.Series(dtype=str)) for p, d in files.items()}
        allv = _canon(pd.concat(list(parts.values()), ignore_index=True), col) if any(len(x) for x in parts.values()) else pd.Series(dtype=str)
        pos = 0
        for p, d in files.items():
            vals = allv.iloc[pos:pos + len(d)]
            pos += len(d)
            if not len(d):
                continue
            keys = d["project"] if p == "GCP" else d["account_name"]
            m = dict(zip(keys, vals))
            if p == "AWS":
                m = {**dict(zip(d["account_id"].str.zfill(12), vals)), **m}
            out[p][col] = m
    return out


def attach_dims(df: pd.DataFrame) -> pd.DataFrame:
    maps = _dim_maps()
    for col in DIM_COLS:
        df[col] = MISSING[col]
        for prov, m in maps.items():
            mm = df["provider"].eq(prov)
            if col in m and mm.any():
                df.loc[mm, col] = df.loc[mm, "account"].map(m[col]).fillna(MISSING[col])
    return df


def attach_owners(df: pd.DataFrame) -> pd.DataFrame:
    maps = _maps()
    for prov, (own, fam) in maps.items():
        m = df["provider"].eq(prov)
        o = df.loc[m, "account"].map(own).replace("", pd.NA)
        f = df.loc[m, "account"].map(fam).replace("", pd.NA)
        df.loc[m, "owner"] = o.fillna(UNASSIGNED)
        df.loc[m, "family"] = f.fillna(df.loc[m, "account"])
    return attach_dims(df)
