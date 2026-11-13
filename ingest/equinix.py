"""Pull Equinix invoices (last ~12 months) with YOUR credentials and save them in the dashboard's own store.

Plain REST (no SDK needed), read-only. Credentials come from the process environment or the env file named in config.toml
[equinix] env_file (default: .env next to the app):  EQUINIX_CLIENT_ID + EQUINIX_CLIENT_SECRET   (or EQUINIX_TOKEN),  optional EQUINIX_URL.
The account number is discovered from your Fabric connections unless [equinix] account_number is set.

    python -m ingest.equinix            # refresh
"""
from __future__ import annotations
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from finops import equinix as eq
from finops import platforms

KEYS = ["EQUINIX_CLIENT_ID", "EQUINIX_CLIENT_SECRET", "EQUINIX_TOKEN", "EQUINIX_URL"]
PAGE = 100
RETRIES = 3
ROUTER_STATES = ["PROVISIONED", "PROVISIONING", "NOT_PROVISIONED", "DEPROVISIONING", "DEPROVISIONED", "PENDING_CONFIGURATION", "CONFIGURING"]


class EquinixError(RuntimeError):
    pass


class Client:
    def __init__(self, creds: dict[str, str]):
        self.api = (creds.get("EQUINIX_URL") or "https://api.equinix.com").rstrip("/")
        self.secrets = [creds.get("EQUINIX_CLIENT_SECRET", ""), creds.get("EQUINIX_TOKEN", "")]
        self.token = creds.get("EQUINIX_TOKEN") or self._login(creds)

    def _call(self, path: str, body=None, method=None, token: str | None = None, timeout: int = 90):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.api + path, method=method or ("POST" if body is not None else "GET"),
                                     data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise EquinixError(platforms.redact(f"HTTP {e.code} on {path.split('?')[0]}: {e.read().decode()[:200]}", self.secrets)) from None
        except urllib.error.URLError as e:
            raise EquinixError(f"Cannot reach {self.api}: {e.reason}") from None

    def _login(self, creds: dict[str, str]) -> str:
        if not (creds.get("EQUINIX_CLIENT_ID") and creds.get("EQUINIX_CLIENT_SECRET")):
            raise EquinixError("No Equinix credentials. Put EQUINIX_CLIENT_ID and EQUINIX_CLIENT_SECRET (or EQUINIX_TOKEN) in the env file "
                               "(config.toml [equinix] env_file) or the environment.")
        out = self._call("/oauth2/v1/token", {"grant_type": "client_credentials", "client_id": creds["EQUINIX_CLIENT_ID"],
                                              "client_secret": creds["EQUINIX_CLIENT_SECRET"]}, timeout=30)
        return out["access_token"]

    def get(self, path: str, body=None, method=None):
        for attempt in range(1, RETRIES + 1):
            try:
                return self._call(path, body, method, self.token)
            except EquinixError as e:
                if str(e).startswith("HTTP 5") and attempt < RETRIES:         # Equinix sometimes answers 5xx for valid requests
                    time.sleep(5 * attempt)
                    continue
                raise

    def paged(self, path: str, body: dict | None = None, label: str = "") -> list[dict]:
        items, offset = [], 0
        sep = "&" if "?" in path else "?"
        while True:
            if body is None:
                page = self.get(f"{path}{sep}offset={offset}&limit={PAGE}")
            else:
                page = self.get(path, {**body, "pagination": {"offset": offset, "limit": PAGE}})
            data = page.get("data") or []
            total = (page.get("pagination") or {}).get("total", len(data))
            items += data
            offset += PAGE
            if label:
                print(f"[{label}] {min(offset, total)} of {total}", flush=True)
            if not data or offset >= total:
                return items


def account_number(c: Client, configured) -> str:
    if configured:
        return str(configured)
    found = c.get("/fabric/v4/connections/search", {"filter": {"property": "/isRemote", "operator": "=", "values": ["true", "false"]},
                                                    "pagination": {"offset": 0, "limit": 1}})
    for conn in found.get("data") or []:
        n = (conn.get("account") or {}).get("accountNumber")
        if n:
            return str(n)
    raise EquinixError("Could not find the account number from Fabric connections. Set [equinix] account_number in config.toml.")


def names(c: Client) -> dict[str, str]:
    """uuid -> name for connections, Cloud Routers and Network Edge nodes. Best effort: a failure only means less readable item names."""
    out: dict[str, str] = {}
    steps = [("connections", lambda: c.paged("/fabric/v4/connections/search", {"filter": {"property": "/isRemote", "operator": "=", "values": ["true", "false"]}})),
             ("cloud routers", lambda: c.paged("/fabric/v4/routers/search", {"filter": {"and": [{"property": "/state", "operator": "IN", "values": ROUTER_STATES}]}})),
             ("network edge", lambda: c.paged("/ne/v1/devices"))]
    for label, fetch in steps:
        try:
            for x in fetch():
                if x.get("uuid") and x.get("name"):
                    out[x["uuid"]] = x["name"]
                for node in ((x.get("clusterDetails") or {}).get("nodes") or []):
                    if node.get("uuid") and node.get("name"):
                        out[node["uuid"]] = node["name"]
        except EquinixError as e:
            print(f"[names] {label}: skipped ({e})", flush=True)
    return out


def refresh() -> tuple[int, int]:
    cfg = platforms.section("equinix")
    creds, where = platforms.credentials("equinix", KEYS)
    print(f"[auth] credentials from the environment or {where}", flush=True)
    c = Client(creds)
    print("[auth] OK", flush=True)
    acct = account_number(c, cfg.get("account_number"))
    print("[names] resolving connection / router / device names …", flush=True)
    nm = names(c)
    print(f"[names] {len(nm)} names", flush=True)
    invoices = (c.get(f"/v2/invoices?accountNumber={urllib.parse.quote(acct)}").get("data")) or []
    print(f"[billing] {len(invoices)} invoices", flush=True)
    lines = c.paged(f"/v2/invoices/details?accountNumber={urllib.parse.quote(acct)}", label="billing lines")
    ln, inv = eq.normalize(invoices, lines, nm)
    if ln.empty:
        raise EquinixError("The API returned no invoice lines.")
    eq.save(ln, inv)
    charges = ln[ln["kind"].eq("charge")]
    platforms.log_refresh(eq.PLATFORM, ln["month"].min(), ln["month"].max(), len(ln), float(charges["amount"].sum()), f"{len(invoices)} invoices")
    print(f"[done] {len(ln)} lines, {ln['month'].min():%Y-%m} to {ln['month'].max():%Y-%m}", flush=True)
    return len(ln), len(invoices)


def main() -> int:
    try:
        refresh()
    except EquinixError as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
