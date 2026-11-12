"""Cloud tags / labels per account or project (NO Streamlit and no cloud calls in here, so it is unit-tested).

The ingest pulls, for the last few billed months, which tag values each AWS account / GCP project carries and how much of its spend
they cover. Three plain CSVs in the data folder (local only, never committed):
  tags_accounts.csv  every account / project with spend in the window           (provider, account_id, account, total, window, pulled_at)
  tags_values.csv    one row per account x tag key x value that has spend        (provider, account_id, account, key, value, cost, window, pulled_at)
  tags_catalog.csv   every tag key found, whether it was pulled, and why not     (provider, key, status, pulled, accounts, values, tagged_cost, note)

Tag values are READ-ONLY reference data in the editor. What you type (cost_center, project_code, treatment ...) lives in the owner files,
so a refresh never overwrites it.
"""
from __future__ import annotations
import fnmatch
import os
from datetime import datetime

import pandas as pd

from .config import load_config, resolve

ACCOUNT_COLS = ["provider", "account_id", "account", "total", "window", "pulled_at"]
VALUE_COLS = ["provider", "account_id", "account", "key", "value", "cost", "window", "pulled_at"]
CATALOG_COLS = ["provider", "key", "status", "pulled", "accounts", "values", "tagged_cost", "note"]
FINANCE_FIELDS = ["cost_center", "project_code"]          # owner-file columns that a cloud tag can fill / be compared with
DEFAULTS = {
    "months": 3,                                           # billed months looked at
    "max_values": 200,                                     # a key with more distinct values is identifier-like (Name, pod ...) and is not kept
    "max_keys": 25,                                        # GCP auto mode: how many label keys to keep, biggest spend first
    "aws": "active",                                       # "active" = every Active user-defined cost allocation tag, or a list of keys
    "gcp": "auto",                                         # "auto" = biggest non-goog labels, or a list of keys
    "exclude": ["Name", "name", "goog-*", "aws:*", "kubernetes.io/*", "k8s*", "*.k8s.io/*", "wiz*", "eks:*", "cloudfix:*"],
    "map": {"cost_center": ["cost-center", "Cost_Center", "CostCenter"], "project_code": ["Project", "project", "Project_Name", "PROJECT"]},
}


# --------------------------------------------------------------------------------------- config / files
def tag_config() -> dict:
    c = load_config().get("tags", {})
    out = {**DEFAULTS, **{k: v for k, v in c.items() if k != "map"}}
    out["map"] = {**DEFAULTS["map"], **{k: ([v] if isinstance(v, str) else list(v)) for k, v in c.get("map", {}).items()}}
    return out


def _dir():
    return resolve(load_config()["data"]["dir"])


def path(kind: str):
    return _dir() / f"tags_{kind}.csv"


def _read(kind: str, cols: list[str]) -> pd.DataFrame:
    p = path(kind)
    if not p.is_file():
        return pd.DataFrame(columns=cols)
    return pd.read_csv(p, dtype={"account_id": str, "key": str, "value": str, "account": str}, keep_default_na=False).reindex(columns=cols)


def read_accounts() -> pd.DataFrame:
    d = _read("accounts", ACCOUNT_COLS)
    d["total"] = pd.to_numeric(d["total"], errors="coerce").fillna(0.0)
    return d


def read_values() -> pd.DataFrame:
    d = _read("values", VALUE_COLS)
    d["cost"] = pd.to_numeric(d["cost"], errors="coerce").fillna(0.0)
    return d


def read_catalog() -> pd.DataFrame:
    d = _read("catalog", CATALOG_COLS)
    for c in ("accounts", "values", "tagged_cost"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    d["pulled"] = d["pulled"].astype(str).str.lower().eq("true")
    return d


def has_tags(provider: str | None = None) -> bool:
    a = read_accounts()
    return bool(len(a) if provider is None else a.provider.eq(provider).sum())


def save_provider(provider: str, accounts: pd.DataFrame, values: pd.DataFrame, catalog: pd.DataFrame) -> None:
    """Replace this provider's rows in the three files, keep the other provider's."""
    _dir().mkdir(parents=True, exist_ok=True)
    for kind, cols, new in (("accounts", ACCOUNT_COLS, accounts), ("values", VALUE_COLS, values), ("catalog", CATALOG_COLS, catalog)):
        cur = _read(kind, cols)
        out = pd.concat([cur[cur["provider"] != provider], new.reindex(columns=cols)], ignore_index=True)
        p = path(kind)
        tmp = p.with_name(p.name + ".tmp")
        out.to_csv(tmp, index=False)
        os.replace(tmp, p)


# --------------------------------------------------------------------------------------- choosing keys
def excluded(key: str, cfg: dict) -> bool:
    k = key.lower()
    return any(fnmatch.fnmatchcase(k, pat.lower()) for pat in cfg["exclude"])


def _listed(cfg: dict, provider: str):
    v = cfg["aws" if provider == "AWS" else "gcp"]
    return None if isinstance(v, str) else [str(x) for x in v]


def aws_keys(known: list[dict], cfg: dict) -> tuple[list[str], dict[str, str]]:
    """Which AWS tag keys to query. known = list_cost_allocation_tags rows (TagKey, Status, Type).
    Returns (keys to pull, {key: why it is not pulled}). An INACTIVE tag cannot be queried in Cost Explorer: it must be activated in
    Billing -> Cost allocation tags first."""
    status = {t["TagKey"]: (t["Status"], t.get("Type", "")) for t in known}
    listed = _listed(cfg, "AWS")
    pull, why = [], {}
    for key, (st, typ) in status.items():
        if listed is not None:
            want = key in listed
        else:
            want = st == "Active" and typ != "AWSGenerated" and not excluded(key, cfg)
        if not want:
            why[key] = ("excluded by config" if st == "Active" and excluded(key, cfg) else
                        "inactive in AWS: activate it in Billing > Cost allocation tags to pull it" if st != "Active" else
                        "not selected (AWS-generated or not in the [tags] list)")
        elif st != "Active":
            why[key] = "inactive in AWS: activate it in Billing > Cost allocation tags to pull it"
        else:
            pull.append(key)
    for key in (listed or []):
        if key not in status:
            why[key] = "listed in config.toml but AWS does not know this tag key"
    return pull, why


# --------------------------------------------------------------------------------------- shaping what the ingest fetched
def build(provider: str, accounts: pd.DataFrame, tag_rows: pd.DataFrame, status: dict[str, str], window: str, cfg: dict | None = None,
          why_not: dict[str, str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """accounts: account_id, account, total.  tag_rows: key, account_id, account, value, cost (summed over the window; only keys to keep).
    status: {key: status text} for EVERY key found (pulled or not).  Returns the three tables, ready for save_provider."""
    cfg = cfg or tag_config()
    why_not = dict(why_not or {})
    now = datetime.now().isoformat(timespec="seconds")
    t = tag_rows.copy()
    t["value"] = t["value"].fillna("").astype(str).str.strip()
    t = t[(t["value"] != "") & (t["cost"].abs() > 0)]
    nvals = t.groupby("key")["value"].nunique()
    for k in nvals[nvals > cfg["max_values"]].index:
        why_not[k] = f"{int(nvals[k])} different values: looks like an identifier (name / id), not a category"
    t = t[~t["key"].isin(why_not)]
    values = t.assign(provider=provider, window=window, pulled_at=now)
    acc = accounts.assign(provider=provider, window=window, pulled_at=now)
    rows = []
    for k, s in status.items():
        g = values[values["key"] == k]
        rows.append({"provider": provider, "key": k, "status": s, "pulled": bool(len(g)), "accounts": g["account"].nunique(),
                     "values": g["value"].nunique(), "tagged_cost": round(float(g["cost"].sum()), 2),
                     "note": why_not.get(k, "" if len(g) else "pulled, no spend carries it in this window")})
    cat = pd.DataFrame(rows, columns=CATALOG_COLS)
    return acc.reindex(columns=ACCOUNT_COLS), values.reindex(columns=VALUE_COLS), cat


def gcp_rows(raw: pd.DataFrame, cfg: dict | None = None) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    """raw: one row per project x label key x value x source ('project' or 'resource') with cost, from the billing export.
    A project-level label wins over a resource-level one for the same project and key.
    Returns (tag_rows for build, status per key, why_not per key)."""
    cfg = cfg or tag_config()
    r = raw.copy()
    r["src_rank"] = r["src"].map({"project": 0, "resource": 1})
    best = r.groupby(["account_id", "key"])["src_rank"].transform("min")
    r = r[r["src_rank"] == best]
    status = {}
    for k, g in r.groupby("key"):
        srcs = set(g["src"])
        status[k] = "label on the project" if srcs == {"project"} else "label on resources" if srcs == {"resource"} else "project and resource label"
    spend = r.groupby("key")["cost"].sum().sort_values(ascending=False)
    listed = _listed(cfg, "GCP")
    why = {}
    if listed is not None:
        keep = [k for k in spend.index if k in listed]
        why = {k: "not in the [tags] gcp list" for k in spend.index if k not in listed}
        for k in listed:
            if k not in spend.index:
                status[k] = "not found"
                why[k] = "listed in config.toml but no spend carries this label in the window"
    else:
        keep = []
        for k in spend.index:
            if excluded(k, cfg):
                why[k] = "excluded by config (Google-managed or noisy)"
            elif len(keep) >= cfg["max_keys"]:
                why[k] = f"outside the {cfg['max_keys']} biggest label keys"
            else:
                keep.append(k)
    out = r[r["key"].isin(keep)][["key", "account_id", "account", "value", "cost"]]
    return out, status, why


# --------------------------------------------------------------------------------------- reading it back
def by_name(accounts: pd.DataFrame) -> pd.DataFrame:
    """One row per provider x account NAME. Two projects can share a display name (different ids); billing and the owner files go by name, so they are one line."""
    if accounts.empty:
        return accounts
    return accounts.groupby(["provider", "account"], as_index=False).agg(account_id=("account_id", "first"), total=("total", "sum"),
                                                                          window=("window", "first"), pulled_at=("pulled_at", "first"))


def dominant(values: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    """Per provider x account x key: the value with the most spend, how many values there are and the share of the account's spend tagged."""
    if values.empty:
        return pd.DataFrame(columns=["provider", "account", "account_id", "key", "value", "n_values", "tagged", "total", "coverage"])
    accounts = by_name(accounts)
    g = values.sort_values("cost", ascending=False)
    top = g.drop_duplicates(["provider", "account", "key"])[["provider", "account", "account_id", "key", "value"]]
    agg = g.groupby(["provider", "account", "key"]).agg(n_values=("value", "nunique"), tagged=("cost", "sum")).reset_index()
    out = top.merge(agg, on=["provider", "account", "key"]).merge(accounts[["provider", "account", "total"]], on=["provider", "account"], how="left")
    out["coverage"] = (out["tagged"] / out["total"].where(out["total"].abs() > 0)).clip(0, 1).fillna(0.0)
    return out


def tag_text(value: str, n_values: int, coverage: float) -> str:
    """'1234-5678', '1234-5678 +2' (more values exist), '... · 13%' (only part of the spend is tagged)."""
    s = value + (f" +{n_values - 1}" if n_values > 1 else "")
    return s + (f" · {coverage:.0%}" if coverage < 0.95 else "")


def account_index(provider: str, accounts: pd.DataFrame) -> tuple[set, dict]:
    a = accounts[accounts["provider"] == provider]
    return set(a["account"]), {str(i).zfill(12): n for i, n in zip(a["account_id"], a["account"]) if str(i)}


def resolve_names(provider: str, table: pd.DataFrame, name_col: str, accounts: pd.DataFrame) -> pd.Series:
    """The billing account name each owner-file row refers to ('' when the tag pull has not seen it). AWS falls back to the account id."""
    names, by_id = account_index(provider, accounts)
    out = []
    for _, r in table.iterrows():
        n = str(r[name_col])
        if n in names:
            out.append(n)
        elif provider == "AWS" and str(r.get("account_id", "")).strip().zfill(12) in by_id:
            out.append(by_id[str(r["account_id"]).strip().zfill(12)])
        else:
            out.append("")
    return pd.Series(out, index=table.index)


def cloud_columns(provider: str, table: pd.DataFrame, name_col: str, keys: list[str], dom: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    """Read-only text columns '☁ <key>' aligned to the owner table's rows."""
    nm = resolve_names(provider, table, name_col, accounts)
    d = dom[dom["provider"] == provider]
    out = pd.DataFrame(index=table.index)
    for k in keys:
        dk = d[d["key"] == k].set_index("account")
        out[f"☁ {k}"] = [tag_text(dk.at[n, "value"], int(dk.at[n, "n_values"]), float(dk.at[n, "coverage"])) if n in dk.index else "" for n in nm]
    return out


def suggestions(provider: str, table: pd.DataFrame, name_col: str, dom: pd.DataFrame, accounts: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """For each FINANCE field (cost_center, project_code) the dominant value of the first mapped tag key that has one: columns '<field>' and '<field>_key'."""
    cfg = cfg or tag_config()
    nm = resolve_names(provider, table, name_col, accounts)
    d = dom[dom["provider"] == provider]
    out = pd.DataFrame(index=table.index)
    for field in FINANCE_FIELDS:
        cands = {c.lower(): i for i, c in enumerate(cfg["map"].get(field, []))}
        dd = d[d["key"].str.lower().isin(cands)].copy()
        dd["_r"] = dd["key"].str.lower().map(cands)
        dd = dd.sort_values(["_r", "tagged"], ascending=[True, False]).drop_duplicates("account").set_index("account")
        out[field] = [dd.at[n, "value"] if n in dd.index else "" for n in nm]
        out[f"{field}_key"] = [dd.at[n, "key"] if n in dd.index else "" for n in nm]
    return out


def check(own: pd.DataFrame, sugg: pd.DataFrame) -> pd.Series:
    """One short text per row comparing what is typed in the owner file with what the cloud tag says ('' when there is nothing to say)."""
    out = []
    for i in own.index:
        bits = []
        for f in FINANCE_FIELDS:
            mine, cloud = str(own.at[i, f]).strip() if f in own else "", str(sugg.at[i, f]) if f in sugg else ""
            if not cloud:
                continue
            if not mine:
                bits.append(f"{f}: cloud has {cloud}")
            elif mine.lower() != cloud.lower():
                bits.append(f"{f}: differs, cloud says {cloud}")
        out.append("; ".join(bits))
    return pd.Series(out, index=own.index)


def prefill(own: pd.DataFrame, sugg: pd.DataFrame) -> pd.DataFrame:
    """A copy of the owner table where EMPTY cost_center / project_code are filled from the cloud tag. Typed values are never replaced."""
    o = own.copy()
    for f in FINANCE_FIELDS:
        if f in o and f in sugg:
            blank = o[f].fillna("").astype(str).str.strip() == ""
            o.loc[blank, f] = sugg.loc[blank, f]
    return o


def coverage(provider: str, field: str, dom: pd.DataFrame, accounts: pd.DataFrame, cfg: dict | None = None) -> dict:
    """How much spend / how many accounts carry a tag mapped to `field`. Uses the best mapped key per account."""
    cfg = cfg or tag_config()
    a = by_name(accounts)
    a = a[a["provider"] == provider]
    cands = {c.lower() for c in cfg["map"].get(field, [])}
    d = dom[(dom["provider"] == provider) & dom["key"].str.lower().isin(cands)]
    best = d.groupby("account")["tagged"].max() if len(d) else pd.Series(dtype=float)
    tagged = a["account"].map(best).fillna(0.0).clip(upper=a["total"].clip(lower=0))
    total = float(a["total"].sum())
    untagged = a.assign(untagged=a["total"] - tagged)[lambda x: x["untagged"] > 0.5].sort_values("untagged", ascending=False)
    keys = sorted(set(d["key"])) if len(d) else []
    return dict(field=field, keys=keys, total=total, tagged=float(tagged.sum()), share=float(tagged.sum() / total) if total else 0.0,
                accounts=len(a), with_tag=int((tagged > 0).sum()), untagged=untagged[["account", "untagged"]].rename(columns={"untagged": "Spend without it"}))
