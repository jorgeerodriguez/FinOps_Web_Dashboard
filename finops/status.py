"""Lifecycle status of every account / project in the owner files (NO Streamlit; unit-tested).

  New      found in billing and not seen before. The refresh adds it to the owner file with this status so you can fill the fields.
  Active   billing is current (spend in the last `quiet_months`).
  Idle     never billed in the history we hold: it may exist, it costs nothing.
  Deleted  was billed before, then silent for `quiet_months` or more (or the deep audit says the provider removed / closed it).
  Orphan   looks abandoned or unowned: set by you, e.g. from the Orphans page.
  Remove   marked to be purged from the owner file (the purge archives the rows, billing history is untouched).

The file holds the status YOU decided. The app computes a SUGGESTED status from billing (and the latest deep audit) and shows it beside it;
Orphan and Remove are only ever set by hand. Billing alone cannot tell "idle" from "deleted": silence is a suggestion until the audit confirms it."""
from __future__ import annotations
import getpass
import re
from datetime import datetime

import numpy as np
import pandas as pd

from . import audit as au
from . import editors as E
from .config import load_config, resolve
from .owners import STATUSES

MANUAL = {"Orphan", "Remove"}                 # never suggested, never overwritten
HIDDEN = {"Deleted", "Remove"}                # left out of the "still missing" counts in the owners editor
LOG_COLS = ["when", "who", "provider", "account", "old", "new", "source", "reason"]
GONE_STATES = {"DELETE_REQUESTED", "DELETE_IN_PROGRESS", "SUSPENDED", "PENDING_CLOSURE", "CLOSED", "DELETED"}


def quiet_months() -> int:
    return int(load_config().get("status", {}).get("quiet_months", 6))


def canon(s) -> str:
    """'active' / ' Active ' -> 'Active'; anything unknown is kept as typed; blank stays blank."""
    v = str(s or "").strip()
    return {x.lower(): x for x in STATUSES}.get(v.lower(), v)


def log_path():
    return resolve(load_config()["data"]["dir"]) / "status_log.csv"


# --------------------------------------------------------------------------------------- facts from billing
def _billed(df: pd.DataFrame, provider: str) -> pd.DataFrame:
    b = df[df["provider"] == provider]
    return b[~b["is_partial"].astype(bool)] if "is_partial" in b else b


def row_keys(provider: str, table: pd.DataFrame, known: set) -> list:
    """The billing account name each owner-file row refers to (by name; AWS also by 12-digit id), None when billing has never seen it."""
    key = E.OWNER_KEY[provider]
    out = []
    for _, r in table.iterrows():
        n = str(r[key])
        if n in known:
            out.append(n)
        elif provider == "AWS" and str(r.get("account_id", "")).strip() and str(r["account_id"]).strip().zfill(12) in known:
            out.append(str(r["account_id"]).strip().zfill(12))
        else:
            out.append(None)
    return out


def facts(provider: str, table: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """Per owner-file row: first / last billed month (a month counts when |net| > $1), months since last billed, spend in the last 3 months."""
    b = _billed(df, provider)
    last = b["date"].max()
    g = b[b["cost_net"].abs() > 1].groupby("account")["date"].agg(first="min", last="max")
    r3 = b[b["date"] > last - pd.DateOffset(months=3)].groupby("account")["cost_net"].sum()
    keys = row_keys(provider, table, set(b["account"]))
    out = pd.DataFrame(index=table.index)
    out["first_billed"] = [g["first"].get(k, pd.NaT) if k else pd.NaT for k in keys]
    out["last_billed"] = [g["last"].get(k, pd.NaT) if k else pd.NaT for k in keys]
    out["spend3"] = [float(r3.get(k, 0.0)) if k else 0.0 for k in keys]
    lb = pd.to_datetime(out["last_billed"])
    out["quiet"] = (last.year - lb.dt.year) * 12 + (last.month - lb.dt.month)
    out["last_month"] = last
    out["never"] = out["last_billed"].isna()
    return out


def audit_gone(provider: str) -> dict:
    """{name or id (lower case): audit date} for accounts / projects the latest deep audit says the provider removed or closed."""
    a = au.latest_audit(provider)
    out = {}
    for r in (a or {}).get("rows", []):
        if r.get("status") == "gone" or str(r.get("state", "")).upper() in GONE_STATES:
            for k in (r.get("name"), r.get("id")):
                if k:
                    out[str(k).lower()] = a.get("as_of", "")
    return out


def is_ready(row) -> bool:
    """Enough filled in to call a New account Active: an owner, a project code and a real environment."""
    g = lambda c: str(row.get(c, "") or "").strip().lower()
    return bool(g("owner")) and bool(g("project_code")) and g("environment") not in ("", "unspecified")


# --------------------------------------------------------------------------------------- suggestions
def suggest(provider: str, table: pd.DataFrame, f: pd.DataFrame, gone: dict | None = None, quiet: int | None = None) -> pd.DataFrame:
    """Per row: the suggested status and why. A suggestion equal to the current status is returned too ('same' = True)."""
    quiet = quiet_months() if quiet is None else quiet
    gone = gone if gone is not None else audit_gone(provider)
    key, idc = E.OWNER_KEY[provider], "account_id"
    sug, why = [], []
    for i, r in table.iterrows():
        cur = canon(r.get("status", ""))
        fr = f.loc[i]
        if cur in MANUAL:
            sug.append(cur)
            why.append(f"marked {cur} but still billing {fr['spend3']:,.0f} in the last 3 months" if fr["spend3"] > 100 else "")
            continue
        confirmed = gone.get(str(r[key]).lower()) or (gone.get(str(r.get(idc, "")).lower()) if r.get(idc) else None)
        if fr["never"] and not confirmed:
            sug.append("Idle"); why.append("never billed in the history we hold (it may exist at $0)")
        elif confirmed:
            sug.append("Deleted"); why.append(f"the deep audit of {confirmed} says the provider removed or closed it")
        elif fr["quiet"] >= quiet:
            sug.append("Deleted"); why.append(f"no spend since {pd.Timestamp(fr['last_billed']):%b %Y} ({int(fr['quiet'])} months): confirm it was retired")
        elif cur == "New" and not is_ready(r):
            sug.append("New"); why.append("")
        elif cur == "New":
            sug.append("Active"); why.append("owner, project code and environment are filled in")
        elif cur == "" and fr["first_billed"] > fr["last_month"] - pd.DateOffset(months=3) and not is_ready(r):
            sug.append("New"); why.append(f"first billed {pd.Timestamp(fr['first_billed']):%b %Y}, fields still to fill")
        else:
            sug.append("Active")
            why.append("billing is current again" if cur in ("Idle", "Deleted") else "")
    out = pd.DataFrame({"suggested": sug, "why": why}, index=table.index)
    out["current"] = [canon(x) for x in table["status"]]
    out["same"] = out["suggested"].eq(out["current"])
    return out


def check_text(s: pd.DataFrame) -> pd.Series:
    """The 'Status check' column: what the suggestion is, only where it differs from what is typed (or a manual status contradicts billing)."""
    out = []
    for r in s.itertuples():
        if not r.same:
            out.append(f"→ {r.suggested}" + (f": {r.why}" if r.why else ""))
        else:
            out.append(r.why)
    return pd.Series(out, index=s.index)


def apply_suggestions(table: pd.DataFrame, s: pd.DataFrame) -> pd.DataFrame:
    """A copy of the table with the suggested status filled in. Orphan / Remove are never touched."""
    t = table.copy()
    t["status"] = [sg if canon(c) not in MANUAL else canon(c) for sg, c in zip(s["suggested"], table["status"])]
    return t


def summary(table: pd.DataFrame) -> dict:
    c = table["status"].map(canon).replace("", "(not set)").value_counts()
    return {k: int(c.get(k, 0)) for k in STATUSES + ["(not set)"]}


# --------------------------------------------------------------------------------------- writing (always backed up through editors.save_csv)
def log_changes(provider: str, rows: list[dict], source: str) -> None:
    if not rows:
        return
    p = log_path()
    new = pd.DataFrame([{**r, "when": datetime.now().isoformat(timespec="seconds"), "who": getpass.getuser(), "provider": provider, "source": source} for r in rows])
    old = pd.read_csv(p, dtype=str, keep_default_na=False) if p.is_file() else pd.DataFrame(columns=LOG_COLS)
    pd.concat([old, new.reindex(columns=LOG_COLS)], ignore_index=True).to_csv(p, index=False)


def diff_log(provider: str, before: pd.DataFrame, after: pd.DataFrame) -> list[dict]:
    """Status changes between two versions of the owner table (matched by name column)."""
    key = E.OWNER_KEY[provider]
    b = dict(zip(before[key], before["status"].map(canon)))
    out = []
    for n, s in zip(after[key], after["status"].map(canon)):
        if n in b and b[n] != s:
            out.append(dict(account=n, old=b[n], new=s, reason=""))
        elif n not in b and s:
            out.append(dict(account=n, old="", new=s, reason="row added"))
    return out


def new_accounts(provider: str, table: pd.DataFrame, df: pd.DataFrame, quiet: int | None = None) -> pd.DataFrame:
    """Accounts that bill but have no row in the owner file, and were billed within the quiet window (older ones are history, not news)."""
    quiet = quiet_months() if quiet is None else quiet
    b = _billed(df, provider)
    if b.empty:
        return pd.DataFrame(columns=["account", "first_billed", "last_billed"])
    last = b["date"].max()
    g = b[b["cost_net"].abs() > 1].groupby("account")["date"].agg(first="min", last="max")
    have = set(table[E.OWNER_KEY[provider]])
    if provider == "AWS":
        have |= {str(x).strip().zfill(12) for x in table["account_id"] if str(x).strip()}
    g = g[[a not in have for a in g.index]]
    g = g[g["last"] > last - pd.DateOffset(months=quiet)]
    return g.reset_index().rename(columns={"first": "first_billed", "last": "last_billed"}).sort_values("first_billed", ascending=False)


def add_new(provider: str, df: pd.DataFrame | None = None, source: str = "refresh") -> list[str]:
    """Append every new account to the owner file as status New (backed up first). Returns the names added."""
    from . import store
    df = store.read() if df is None else df
    table = E.read_owner_file(provider)
    new = new_accounts(provider, table, df)
    if new.empty:
        return []
    rows = pd.DataFrame({E.OWNER_KEY[provider]: new["account"].values})
    if provider == "AWS":
        rows["account_id"] = [m.group(1) if (m := re.match(r"^(\d{12})\b", a)) else "" for a in new["account"]]
    rows["status"] = "New"
    base = table.drop(columns=[E.RID])
    out = pd.concat([base, rows.reindex(columns=base.columns)], ignore_index=True).fillna("")
    E.save_csv(E.owner_path(provider), out)
    log_changes(provider, [dict(account=a, old="", new="New", reason=f"first billed {pd.Timestamp(d):%b %Y}") for a, d in zip(new["account"], new["first_billed"])], source)
    return list(new["account"])


def set_status(provider: str, names: list[str], status: str, source: str, reason: str = "") -> int:
    """Set `status` on the named rows of the owner file (rows that are not in the file are added). Returns how many rows changed."""
    table = E.read_owner_file(provider)
    key = E.OWNER_KEY[provider]
    base = table.drop(columns=[E.RID]).copy()
    log, add = [], []
    for n in names:
        hit = base.index[base[key] == n]
        if len(hit):
            old = canon(base.loc[hit[0], "status"])
            if old != status:
                base.loc[hit[0], "status"] = status
                log.append(dict(account=n, old=old, new=status, reason=reason))
        else:
            add.append(n)
            log.append(dict(account=n, old="", new=status, reason=reason or "row added"))
    if add:
        extra = pd.DataFrame({key: add, "status": status}).reindex(columns=base.columns)
        base = pd.concat([base, extra], ignore_index=True).fillna("")
    if log:
        E.save_csv(E.owner_path(provider), base)
        log_changes(provider, log, source)
    return len(log)


def purge(provider: str) -> int:
    """Move every row marked Remove to data/archive/ and drop it from the owner file. Billing history is not touched."""
    table = E.read_owner_file(provider)
    gone = table["status"].map(canon).eq("Remove")
    if not gone.any():
        return 0
    arch = resolve(load_config()["data"]["dir"]) / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    table[gone].drop(columns=[E.RID]).to_csv(arch / f"owners_{provider.lower()}_removed_{datetime.now():%Y%m%d-%H%M%S}.csv", index=False)
    E.save_csv(E.owner_path(provider), table[~gone].drop(columns=[E.RID]))
    log_changes(provider, [dict(account=n, old="Remove", new="(purged)", reason="archived to data/archive") for n in table[gone][E.OWNER_KEY[provider]]], "purge")
    return int(gone.sum())
