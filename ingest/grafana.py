"""Pull Grafana Cloud usage cost with the STACK service-account token and save it in the dashboard's own store.

Read-only. Everything comes from the stack's pre-provisioned ``grafanacloud-usage`` Prometheus datasource (through the Grafana
datasource proxy) plus ``grafanacloud-usage-insights`` for activity counts. No Cloud Access Policy token is needed.

Credentials come from the process environment or the env file named in config.toml [grafana] env_file (default: .env next to the app):
    GRAFANA_STACK_TOKEN   a read-only service-account token (glsa_...) with datasource query access
    GRAFANA_URL           optional here; otherwise config.toml [grafana] url, e.g. https://yourorg.grafana.net

    python -m ingest.grafana
"""
from __future__ import annotations
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

import pandas as pd

from finops import grafana as gf
from finops import platforms

KEYS = ["GRAFANA_STACK_TOKEN", "GRAFANA_URL"]
PREFIX, TOTAL = "grafanacloud_org_", "grafanacloud_org_total_overage"
HISTORY_DAYS = 400
RETRIES = 4


class GrafanaError(RuntimeError):
    pass


class Client:
    def __init__(self, url: str, token: str):
        if not token:
            raise GrafanaError("No Grafana token. Put GRAFANA_STACK_TOKEN (a read-only service-account token) in the env file "
                               "(config.toml [grafana] env_file) or the environment.")
        if not url:
            raise GrafanaError("No Grafana URL. Set [grafana] url in config.toml (for example https://yourorg.grafana.net) or GRAFANA_URL.")
        self.url, self.token = url.rstrip("/"), token

    def get(self, path: str, params: dict | None = None):
        full = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        delay = 1.0
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(full, headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise GrafanaError(f"{e.code} from {path.split('?')[0]}: the token is invalid or cannot query datasources.") from None
                if e.code not in (429, 500, 502, 503, 504) or attempt == RETRIES:
                    raise GrafanaError(platforms.redact(f"HTTP {e.code} from {path.split('?')[0]}: {e.read().decode()[:160]}", [self.token])) from None
            except urllib.error.URLError as e:
                if attempt == RETRIES:
                    raise GrafanaError(f"Cannot reach {self.url}: {e.reason}") from None
            time.sleep(delay)
            delay = min(delay * 2, 20)
        raise GrafanaError("retries exhausted")


class Usage:
    """Prometheus-style queries against the usage datasource."""

    def __init__(self, c: Client, uid: str = "grafanacloud-usage"):
        self.c, self.base = c, f"/api/datasources/proxy/uid/{uid}/api/v1"

    def instant(self, expr: str) -> float | None:
        res = self.c.get(f"{self.base}/query", {"query": expr})["data"]["result"]
        return float(res[0]["value"][1]) if res else None

    def range(self, expr: str, start: datetime, end: datetime, step: int) -> pd.Series:
        """Chunks of at most 9000 points; returns a naive-UTC indexed series."""
        pts: dict[int, float] = {}
        cur, chunk = start, timedelta(seconds=step * 9000)
        while cur < end:
            nxt = min(cur + chunk, end)
            res = self.c.get(f"{self.base}/query_range", {"query": expr, "start": int(cur.timestamp()), "end": int(nxt.timestamp()), "step": step})["data"]["result"]
            for ts, val in (res[0]["values"] if res else []):
                pts[int(ts)] = float(val)
            cur = nxt + timedelta(seconds=step)
        s = pd.Series(list(pts.values()), index=pd.to_datetime(list(pts.keys()), unit="s", utc=True), dtype="float64").sort_index()
        s.index = s.index.tz_localize(None)
        return s

    def components(self) -> list[str]:
        names = self.c.get(f"{self.base}/label/__name__/values")["data"]
        return sorted(n for n in names if n.startswith(PREFIX) and n.endswith("_overage") and n != TOTAL)


def collect_costs(u: Usage, now: datetime) -> tuple[pd.DataFrame, dict]:
    end, start = now, now - timedelta(days=HISTORY_DAYS)
    this_month = pd.Period(now.date(), "M")
    rows, parts = [], {}
    for metric in u.components() + [TOTAL]:
        s = u.range(f"sum({metric})", start, end, 3600)
        if s.empty:
            continue
        last = s.groupby(s.index.to_period("M")).last()               # running overage: the month's last value is its cost
        key = metric.removeprefix(PREFIX).removesuffix("_overage")
        for per, v in last.items():
            if per < this_month:
                rows.append(dict(month=per.start_time, component=key, product=gf.product_for(key), cost=float(v)))
    df = pd.DataFrame(rows, columns=gf.COST_COLS)
    return df, parts


def collect_drivers(u: Usage, now: datetime) -> pd.DataFrame:
    start = now - timedelta(days=HISTORY_DAYS)
    rows = []
    for name, (unit, expr) in gf.DRIVERS.items():
        s = u.range(expr, start, now, 86400)
        if s.empty:
            continue
        s.index = s.index.normalize() - pd.Timedelta(days=1)          # the value covers the prior day
        for per, v in s.groupby(s.index.to_period("M")).mean().items():
            rows.append(dict(month=per.start_time, driver=name, unit=unit, value=float(v)))
    return pd.DataFrame(rows, columns=gf.DRV_COLS)


def collect_facts(u: Usage, now: datetime) -> tuple[dict, pd.DataFrame]:
    facts: dict = {"as_of": now.date().isoformat()}
    facts["current_month"] = str(pd.Period(now.date(), "M"))
    facts["current_estimate"] = u.instant(f"sum({TOTAL})")
    facts["commit_total"] = u.instant("sum(grafanacloud_org_spend_commit_credit_total)")
    facts["commit_balance"] = u.instant("sum(grafanacloud_org_spend_commit_balance_total)")
    for key, metric in (("contract_start", "grafanacloud_org_contract_start_date"), ("contract_end", "grafanacloud_org_contract_end_date")):
        v = u.instant(f"max({metric})")
        facts[key] = datetime.fromtimestamp(v, UTC).date().isoformat() if v else None
    facts["recommended_savings_series"] = u.instant("sum(grafanacloud_instance_recommendations_estimated_savings_series)")
    facts["active_series_now"] = u.instant("sum(grafanacloud_instance_active_series)")
    bal = u.range("sum(grafanacloud_org_spend_commit_balance_total)", now - timedelta(days=HISTORY_DAYS), now, 86400)
    bal.index = bal.index.normalize()
    hist = pd.DataFrame({"month": bal.index, "balance": bal.values}).drop_duplicates("month", keep="last")
    return facts, hist


def collect_activity(c: Client) -> dict:
    """Counts only (no user identifiers are read). Each part is best effort."""
    sel = '{instance_type="grafana"} |= "Usage insights event" | logfmt'
    loki = "/api/datasources/proxy/uid/grafanacloud-usage-insights/loki/api/v1/query"

    def vec(expr):
        return c.get(loki, {"query": expr})["data"]["result"]

    out: dict = {"active_users": {}, "folders_30d": {}, "notes": []}
    for days in (7, 30, 90):
        try:
            r = vec(f'count(sum by (userId)(count_over_time({sel} | userId!="" [{days}d])))')
            out["active_users"][str(days)] = int(float(r[0]["value"][1])) if r else 0
        except GrafanaError as e:
            out["notes"].append(f"active users ({days}d): {e}")
    try:
        for r in vec(f'sum by (folderName)(count_over_time({sel} | eventName="data-request" [30d]))'):
            name = r["metric"].get("folderName") or "General (no folder)"
            out["folders_30d"][name] = out["folders_30d"].get(name, 0) + int(float(r["value"][1]))
    except GrafanaError as e:
        out["notes"].append(f"activity by folder: {e}")
    try:
        r = vec(f'count(sum by (dashboardUid)(count_over_time({sel} | dashboardUid!="" [90d])))')
        out["dashboards_active_90d"] = int(float(r[0]["value"][1])) if r else 0
        total, page = 0, 1
        while True:
            rows = c.get("/api/search", {"type": "dash-db", "limit": 5000, "page": page})
            total += len(rows)
            if len(rows) < 5000:
                break
            page += 1
        out["dashboards_total"] = total
    except GrafanaError as e:
        out["notes"].append(f"dashboard usage: {e}")
    return out


def refresh() -> int:
    cfg = platforms.section("grafana")
    creds, where = platforms.credentials("grafana", KEYS)
    print(f"[auth] credentials from the environment or {where}", flush=True)
    c = Client(creds.get("GRAFANA_URL") or cfg.get("url", ""), creds.get("GRAFANA_STACK_TOKEN", ""))
    u = Usage(c, cfg.get("usage_datasource", "grafanacloud-usage"))
    now = datetime.now(UTC).replace(tzinfo=None)
    now = now.replace(minute=0, second=0, microsecond=0)
    print("[costs] reading monthly overage per product (about 400 days) …", flush=True)
    costs, _ = collect_costs(u, now)
    if costs.empty:
        raise GrafanaError("The usage datasource returned no cost metrics.")
    print(f"[costs] {costs['component'].nunique()} components, {costs['month'].nunique()} complete months", flush=True)
    print("[drivers] active series, log and trace volume, users …", flush=True)
    drivers = collect_drivers(u, now)
    print("[commit] commit balance, contract dates, current month …", flush=True)
    facts, bal = collect_facts(u, now)
    try:
        print("[activity] active users, dashboards, folders (counts only) …", flush=True)
        facts["activity"] = collect_activity(c)
    except GrafanaError as e:
        facts["activity"] = {"notes": [str(e)]}
    gf.save(costs, drivers, bal, facts)
    total = float(costs[costs["component"].ne("total")]["cost"].sum())
    platforms.log_refresh(gf.PLATFORM, costs["month"].min(), costs["month"].max(), len(costs), total, f"{costs['component'].nunique()} components")
    print(f"[done] {len(costs)} cost rows, {costs['month'].min():%Y-%m} to {costs['month'].max():%Y-%m}", flush=True)
    return len(costs)


def main() -> int:
    try:
        refresh()
    except GrafanaError as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
