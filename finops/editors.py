"""Logic behind the in-app editors for the owner files and the anomaly rules (NO Streamlit in here, so it is unit-tested).

The files stay plain CSVs you can still open anywhere. Writing is careful:
  - every save first copies the current file to <data dir>/backups/ (the newest 20 are kept)
  - the new file is written to a temp name and swapped in, so a crash never leaves half a file
  - columns the editor does not show (e.g. exit run-rate notes in the owner files) are written back untouched
  - the editor refuses to save if the file changed on disk after it was opened (someone else, or Excel, saved it)"""
from __future__ import annotations
import difflib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .config import load_config, resolve
from .owners import STATUSES

FINANCE_COLS = ["cost_center", "project_code", "treatment", "finance_notes"]       # typed by you; cloud tags are only shown beside them
TREATMENTS = ["Opex", "Capex-eligible", "Prepaid"]
OWNER_CORE = {"AWS": ["account_id", "account_name", "status", "family", "environment", "service_name", "owner", "department"] + FINANCE_COLS,
              "GCP": ["project", "status", "family", "environment", "owner", "department"] + FINANCE_COLS}
OWNER_KEY = {"AWS": "account_name", "GCP": "project"}
RULE_COLS = ["provider", "account", "service", "months", "action", "note"]
RULE_ACTIONS = ["add_back", "confirmed_real", "pending"]
KEEP_BACKUPS = 20
RID = "_rid"                      # hidden column: the row's position in the file when it was loaded (NaN = a row added in the editor)


# --------------------------------------------------------------------------------------- files
def owner_path(provider: str) -> Path:
    return resolve(load_config().get("owners", {}).get("aws_csv" if provider == "AWS" else "gcp_csv", f"data/owners_{provider.lower()}.csv"))


def rules_path() -> Path:
    return resolve(load_config().get("forecast", {}).get("adjustments_csv", "data/anomaly_rules.csv"))


def mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def read_csv_text(path: Path, core: list[str]) -> pd.DataFrame:
    """Everything as text, whitespace trimmed, all core columns present. A missing file is an empty table."""
    if not path.is_file():
        return pd.DataFrame(columns=core, dtype=str)
    d = pd.read_csv(path, dtype=str, keep_default_na=False)
    d = d.apply(lambda c: c.str.strip())
    for c in core:
        if c not in d.columns:
            d[c] = ""
    return d


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    d = path.parent / "backups"
    d.mkdir(exist_ok=True)
    dest = d / f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S-%f}{path.suffix}"
    shutil.copy2(path, dest)
    for old in sorted(d.glob(f"{path.stem}.*{path.suffix}"))[:-KEEP_BACKUPS]:
        old.unlink()
    return dest


def save_csv(path: Path, df: pd.DataFrame) -> Path | None:
    """Back up, then write atomically. Returns the backup path (None if there was no file yet)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    b = backup(path)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return b


class Stale(Exception):
    """The file changed on disk after the editor loaded it."""


def check_fresh(path: Path, loaded_mtime: float) -> None:
    if abs(mtime(path) - loaded_mtime) > 1e-6:
        raise Stale(f"{path.name} changed on disk after you opened it (another save, or Excel). Reload the page to see the current version, then redo your edit.")


# --------------------------------------------------------------------------------------- owners
def read_owner_file(provider: str) -> pd.DataFrame:
    d = read_csv_text(owner_path(provider), OWNER_CORE[provider])
    cols = [c for c in d.columns if c != "status"]
    cols.insert(cols.index(OWNER_KEY[provider]) + 1, "status")                 # status sits right after the name column, wherever the file had it
    d = d[cols]
    d[RID] = np.arange(len(d), dtype=float)
    return d


def spend_by_account(df: pd.DataFrame, provider: str) -> pd.Series:
    """Last 12 billed months, ALL categories (an owner is responsible for the whole account), keyed by the account/project as billed."""
    d = df[(df.provider == provider) & ~df.is_partial.astype(bool)]
    d = d[d.date > d.date.max() - pd.DateOffset(months=12)] if len(d) else d
    return d.groupby("account").cost_net.sum()


def row_spend(table: pd.DataFrame, spend: pd.Series, provider: str) -> pd.Series:
    """Spend for each row of the owner file (matched by name, and by id for AWS), $0 when the account is not in billing."""
    name = table[OWNER_KEY[provider]]
    s = name.map(spend)
    if provider == "AWS":
        s = s.fillna(table["account_id"].str.zfill(12).map(spend)).fillna(table["account_id"].map(spend))
    return s.fillna(0.0)


def missing_accounts(table: pd.DataFrame, spend: pd.Series, provider: str, min_spend: float = 1.0) -> pd.DataFrame:
    """Accounts/projects that are billing but have no row in the owner file at all (they show as '(unassigned)')."""
    have = set(table[OWNER_KEY[provider]])
    if provider == "AWS":
        have |= set(table["account_id"]) | set(table["account_id"].str.zfill(12))
    rows = [{OWNER_KEY[provider]: a} for a, v in spend.items() if v >= min_spend and a not in have]
    out = pd.DataFrame(rows, columns=[OWNER_KEY[provider]])
    for c in OWNER_CORE[provider]:
        if c not in out:
            out[c] = ""
    out[RID] = np.nan
    return out[OWNER_CORE[provider] + [RID]].assign(_spend=[spend[a] for a in out[OWNER_KEY[provider]]]).sort_values("_spend", ascending=False).drop(columns="_spend").reset_index(drop=True)


def validate_owners(provider: str, table: pd.DataFrame) -> tuple[list[str], list[str]]:
    """(errors that block the save, warnings that do not)."""
    key = OWNER_KEY[provider]
    errors, warns = [], []
    t = table.copy()
    t[key] = t[key].fillna("").astype(str).str.strip()
    if (t[key] == "").any():
        errors.append(f"{int((t[key] == '').sum())} row(s) have no `{key}`. Fill it in or delete the row.")
    dup = t[t[key] != ""][key]
    dup = dup[dup.duplicated()].unique()
    if len(dup):
        errors.append("Duplicate `" + key + "`: " + ", ".join(map(str, dup[:5])) + ". Each account / project may appear once.")
    if provider == "AWS":
        ids = t["account_id"].fillna("").astype(str).str.strip()
        dids = ids[(ids != "") & ids.duplicated()].unique()
        if len(dids):
            errors.append("Duplicate account_id: " + ", ".join(dids[:5]) + ".")
        rounded = ids[ids.str.contains(r"E\+|\.", regex=True) | ids.str.endswith("000000")]
        if len(rounded):
            warns.append(f"{len(rounded)} account_id value(s) look rounded by Excel (e.g. {rounded.iloc[0]}). Matching still works through the account name, "
                         "but the id is not usable.")
    st_ = t["status"].fillna("").astype(str).str.strip() if "status" in t else pd.Series(dtype=str)
    odd_s = st_[(st_ != "") & ~st_.str.lower().isin([x.lower() for x in STATUSES])]
    if len(odd_s):
        warns.append(f"{len(odd_s)} row(s) have a status that is not one of {', '.join(STATUSES)} (e.g. “{odd_s.iloc[0]}”). Saved as typed.")
    t_ = t["treatment"].fillna("").astype(str).str.strip() if "treatment" in t else pd.Series(dtype=str)
    odd = t_[(t_ != "") & ~t_.str.lower().isin([x.lower() for x in TREATMENTS])]
    if len(odd):
        warns.append(f"{len(odd)} row(s) have a treatment that is not one of {', '.join(TREATMENTS)} (e.g. “{odd.iloc[0]}”). Saved as typed.")
    return errors, warns


def near_duplicate_owners(owners: pd.Series) -> list[tuple[str, str]]:
    """Owner names that differ only by case, spacing or a typo: (rare spelling, common spelling)."""
    o = owners.fillna("").astype(str).str.strip()
    counts = o[o != ""].value_counts()
    out = []
    names = list(counts.index)
    for i, a in enumerate(names):
        for b in names[:i]:
            if a.lower() == b.lower() or difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.88:
                out.append((a, b))
                break
    return out


def merge_owner_edits(original: pd.DataFrame, edited: pd.DataFrame, shown_rids: set, provider: str) -> tuple[pd.DataFrame, dict]:
    """Apply what the editor returned to the full file.
    original  the file as loaded (with RID); edited  the editor's result (core columns + RID);
    shown_rids  the RIDs that were on screen (a filtered view must not delete what it never showed)."""
    core, key = OWNER_CORE[provider], OWNER_KEY[provider]
    res = original.copy()
    e = edited.copy()
    for c in core:
        e[c] = e[c].fillna("").astype(str).str.strip()
    kept = e[e[RID].notna()]
    upd = kept.set_index(RID)[core]
    base = res.set_index(RID)
    changed = 0
    for rid in upd.index:
        if rid in base.index:
            old = base.loc[rid, core].fillna("").astype(str).str.strip()
            if not (old.values == upd.loc[rid].values).all():
                changed += 1
            base.loc[rid, core] = upd.loc[rid].values
    deleted = [r for r in shown_rids if r not in set(kept[RID])]
    base = base.drop(index=[r for r in deleted if r in base.index])
    res = base.reset_index()
    new = e[e[RID].isna() & (e[key] != "")]
    new = new[(new["owner"] != "") | (new[core].ne("").sum(axis=1) > 1)]         # a bare name with no owner and nothing else is not worth a row
    added = len(new)
    if added:
        for c in res.columns:
            if c not in new.columns:
                new[c] = ""
        res = pd.concat([res, new[res.columns]], ignore_index=True)
    out = res.drop(columns=[RID])
    cols = [c for c in original.columns if c != RID]
    return out[cols], dict(changed=changed, added=added, deleted=len(deleted))


# --------------------------------------------------------------------------------------- anomaly rules
def read_rules() -> pd.DataFrame:
    d = read_csv_text(rules_path(), RULE_COLS)
    d = d[RULE_COLS + [c for c in d.columns if c not in RULE_COLS]]
    d[RID] = np.arange(len(d), dtype=float)
    return d


def validate_rules(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    errors, warns = [], []
    d = df.copy()
    for c in RULE_COLS:
        d[c] = d[c].fillna("").astype(str).str.strip()
    bad = d[~d.provider.isin(["AWS", "GCP"])]
    if len(bad):
        errors.append(f"{len(bad)} row(s) need a provider (AWS or GCP).")
    for c in ("account", "service"):
        if (d[c] == "").any():
            errors.append(f"{int((d[c] == '').sum())} row(s) have no {c}.")
    bad = d[~d.action.str.lower().isin(RULE_ACTIONS)]
    if len(bad):
        errors.append(f"{len(bad)} row(s) have an action that is not one of {', '.join(RULE_ACTIONS)}.")
    mm = d.months.map(lambda s: bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])(;\s*\d{4}-(0[1-9]|1[0-2]))*", s)) if s else False)
    bad = d[(d.months != "") & ~mm]
    if len(bad):
        errors.append("`months` must look like 2026-02;2026-03 (YYYY-MM separated by ;). Check: " + ", ".join(bad.months.head(3)) + ".")
    miss = d[(d.action.str.lower() == "add_back") & (d.months == "")]
    if len(miss):
        errors.append(f"{len(miss)} add_back row(s) need the gap months (an add-back with no months does nothing).")
    key = d[["provider", "account", "service"]].apply(tuple, axis=1)
    dup = key[key.duplicated()]
    if len(dup):
        warns.append(f"{len(dup)} line(s) appear twice; only the FIRST row for a provider / account / service is used.")
    return errors, warns


def suggest_rules(anomalies: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Detected lines still pending review that are not in the rules file yet, as ready-to-add `pending` rows.
    Needs the `_service` and `_months` columns find_anomalies adds (the display name is shortened)."""
    if anomalies is None or anomalies.empty or "_service" not in anomalies:
        return pd.DataFrame(columns=["Add"] + RULE_COLS + ["Type", "Missing vs typical"])
    have = set(zip(rules.provider, rules.account, rules.service)) if len(rules) else set()
    a = anomalies[anomalies["Status"] == "Pending review"]
    out = []
    for _, r in a.iterrows():
        if (r["Provider"], r["Account / Project"], r["_service"]) in have:
            continue
        action = "add_back" if r["Type"] == "Gap" else "pending"
        out.append({"Add": False, "provider": r["Provider"], "account": r["Account / Project"], "service": r["_service"], "months": r["_months"],
                    "action": action, "note": "", "Type": r["Type"], "Missing vs typical": float(r["Missing vs typical"])})
    return pd.DataFrame(out, columns=["Add"] + RULE_COLS + ["Type", "Missing vs typical"])


def merge_rule_edits(original: pd.DataFrame, edited: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """The rules table is small and fully shown, so the editor's result simply becomes the file (blank rows dropped)."""
    e = edited.copy()
    for c in RULE_COLS:
        e[c] = e[c].fillna("").astype(str).str.strip()
    e = e[(e[RULE_COLS] != "").any(axis=1)]
    e["action"] = e["action"].str.lower()
    extra = [c for c in original.columns if c not in RULE_COLS + [RID]]
    for c in extra:
        e[c] = e[c] if c in e else ""
    out = e[RULE_COLS + extra].reset_index(drop=True)
    old = original[RULE_COLS].fillna("").astype(str).apply(lambda c: c.str.strip())
    stats = dict(rows=len(out), changed=int(len(out) != len(old) or not out[RULE_COLS].equals(old.reset_index(drop=True))))
    return out, stats
