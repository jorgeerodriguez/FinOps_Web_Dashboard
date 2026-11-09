"""Attach owner / family from your hand-maintained owner CSVs (blank owner -> '(unassigned)')."""
from __future__ import annotations
import pandas as pd
from .config import load_config, resolve

UNASSIGNED = "(unassigned)"


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


def attach_owners(df: pd.DataFrame) -> pd.DataFrame:
    maps = _maps()
    for prov, (own, fam) in maps.items():
        m = df["provider"].eq(prov)
        o = df.loc[m, "account"].map(own).replace("", pd.NA)
        f = df.loc[m, "account"].map(fam).replace("", pd.NA)
        df.loc[m, "owner"] = o.fillna(UNASSIGNED)
        df.loc[m, "family"] = f.fillna(df.loc[m, "account"])
    return df
