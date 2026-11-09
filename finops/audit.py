"""Orphaned accounts / projects: cost-based screening (works from the store, no credentials) + audit result store."""
from __future__ import annotations
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .config import load_config, resolve

STATUS_ORDER = {"spend": 0, "idle": 1, "gone": 2, "review": 3, "active": 4}
STATUS_LABEL = {"spend": "Spend, no owner activity", "idle": "Idle", "gone": "No access / gone",
                "review": "Review", "active": "In use"}
STATUS_COLOR = {"spend": "#dc2626", "idle": "#f59e0b", "gone": "#64748b", "review": "#2563eb", "active": "#16a34a"}


def audit_dir() -> Path:
    d = resolve(load_config().get("audit", {}).get("dir", "data/audits"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def idle_threshold() -> float:
    return float(load_config().get("audit", {}).get("idle_below_usd", 1))


# ------------------------------------------------------------------------------ audit result store (JSON per run)
def save_audit(provider: str, rows: list[dict], note: str = "") -> Path:
    p = audit_dir() / f"{provider.lower()}_{date.today().isoformat()}.json"
    p.write_text(json.dumps({"provider": provider, "as_of": date.today().isoformat(), "note": note, "rows": rows}, default=str))
    return p


def latest_audit(provider: str) -> dict | None:
    files = sorted(audit_dir().glob(f"{provider.lower()}_*.json"))
    return json.loads(files[-1].read_text()) if files else None


def list_audits(provider: str) -> list[Path]:
    return sorted(audit_dir().glob(f"{provider.lower()}_*.json"), reverse=True)


def audit_frame(rows: list[dict]) -> pd.DataFrame:
    keep = ["name", "id", "status", "state", "cost12", "avg3", "last_billed", "last_activity", "days_activity", "n_access", "action"]
    df = pd.DataFrame(rows)
    for c in keep:
        if c not in df:
            df[c] = None
    df["status_label"] = df["status"].map(STATUS_LABEL)
    df["_o"] = df["status"].map(STATUS_ORDER)
    return df.sort_values(["_o", "cost12"], ascending=[True, False]).drop(columns="_o")


# ------------------------------------------------------------------------------ cost-based screening
def screening(df: pd.DataFrame, provider: str) -> pd.DataFrame:
    """One row per account/project from billed data (no credentials). Signals, not verdicts: they say where to look."""
    d = df[(df["provider"] == provider) & (df["category"] != "Tax")]
    if d.empty:
        return pd.DataFrame()
    last = d["date"].max()
    months = pd.date_range(last - pd.DateOffset(months=11), last, freq="MS")
    piv = d[d["date"].isin(months)].pivot_table(index="account", columns="date", values="cost_net", aggfunc="sum").reindex(columns=months).fillna(0.0)
    meta = d.groupby("account").agg(owner=("owner", "first"), family=("family", "first"), first_seen=("date", "min"))
    top_svc = (d[d["date"] > last - pd.DateOffset(months=3)].groupby(["account", "service"])["cost_net"].sum().reset_index()
               .sort_values("cost_net").drop_duplicates("account", keep="last").set_index("account")["service"])
    thr = idle_threshold()
    out = pd.DataFrame({
        "cost12": piv.sum(axis=1), "avg3": piv.iloc[:, -3:].mean(axis=1), "prior3": piv.iloc[:, -6:-3].mean(axis=1),
        "months_billed": (piv > thr).sum(axis=1),
    })
    billed = piv.gt(thr)
    out["last_billed"] = [(piv.columns[np.where(r)[0][-1]] if r.any() else pd.NaT) for r in billed.values]
    out = out.join(meta).join(top_svc.rename("top_service"))
    unowned = out["owner"].fillna("(unassigned)").eq("(unassigned)")
    months_quiet = ((last.year - pd.to_datetime(out["last_billed"]).dt.year) * 12 + (last.month - pd.to_datetime(out["last_billed"]).dt.month))

    def signal(r, uq, q):
        if r.cost12 <= thr * 1:                         # nothing in 12 months
            return "No spend in 12 months"
        if q is not None and q >= 3 and r.avg3 < thr:
            return f"Dormant — last billed {pd.Timestamp(r.last_billed):%b %Y}"
        if r.avg3 < 10:
            return "Near-zero spend (<$10/mo)" + (", no owner" if uq else "")
        if uq:
            return "Spend, no owner on file"
        if r.prior3 > 0 and r.avg3 < 0.3 * r.prior3:
            return "Spend collapsing (>70% drop)"
        return "Owned & billing"

    out["signal"] = [signal(r, u, (None if pd.isna(q) else q)) for (r, u, q) in zip(out.itertuples(), unowned, months_quiet)]
    out["unowned"] = unowned
    # "Orphan-like" = looks abandoned (little or no spend). "Ownership gap" = real spend nobody has claimed (not a saving).
    out["kind"] = np.select([out["signal"].eq("Owned & billing"), out["signal"].eq("Spend, no owner on file")],
                            ["OK", "Ownership gap"], default="Orphan-like")
    out["annualized_at_stake"] = np.where(out["kind"].eq("Orphan-like"), out["avg3"] * 12, 0.0)
    out["unowned_annual_spend"] = np.where(out["kind"].eq("Ownership gap"), out["avg3"] * 12, 0.0)
    out["owner"] = out["owner"].fillna("(unassigned)")
    out = out.reset_index()
    out["_k"] = out["kind"].map({"Orphan-like": 0, "Ownership gap": 1, "OK": 2})
    return out.sort_values(["_k", "annualized_at_stake", "cost12"], ascending=[True, False, False]).drop(columns="_k")
