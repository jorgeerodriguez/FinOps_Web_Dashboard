"""GCP orphaned-project deep audit, run with the CURRENT USER's Application Default Credentials.

Ported from GCP_Orphaned_Projects_Audit.ipynb:
  cost     BigQuery billing export (13 invoice months, services, lifetime)
  access   Resource Manager project + IAM policy
  usage    Cloud Audit Logs: changes by people (Admin Activity), hands-on reads (Data Access, 30 days), service accounts
Log queries are rate limited (60 reads/min): ~15-20 minutes for ~30 projects.
"""
from __future__ import annotations
import re
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from dateutil.relativedelta import relativedelta

from finops.audit import idle_threshold
from finops.config import load_config

# Calls that fire for every project when someone opens the console/billing pages (and this audit's own reads).
NOISE = re.compile(r"(GetResourceBillingInfo|GetProject$|Projects\.GetProject|SearchProjects|GetIamPolicy|"
                   r"ListLogEntries|GetEffectivePolicy|GetEffectiveOrgPolicy|ListEffectiveTags|SearchAllResources|"
                   r"getIamPermissions|TestIamPermissions|ParseTimeSeriesFilter|ListProjects|GetOrganization|"
                   r"ListFolders|GetFolder)", re.I)
HUMAN = 'protoPayload.authenticationInfo.principalEmail:"@" AND NOT protoPayload.authenticationInfo.principalEmail:"gserviceaccount.com"'


def read_names(path: str) -> list[str]:
    return [l.strip() for l in open(path) if l.strip()]


def _months(as_of: date) -> list[str]:
    first = as_of.replace(day=1)
    return [(first - relativedelta(months=i)).strftime("%Y%m") for i in range(12, -1, -1)]


def classify(visible: bool, avg3: float, days_human, last_row_days: int, last_row_date) -> tuple[str, str]:
    """Same decision table as the notebook (see Classify each project)."""
    idle = idle_threshold()
    if not visible:
        if avg3 >= idle:
            return "spend", (f"Still billing about ${avg3:,.0f}/mo, but our account cannot read this project. "
                             "Ask an org admin to identify the owner and confirm it is needed")
        if last_row_days <= 30:
            return "gone", "Still exists (billing records this month) but our account has no access. Ask an org admin who owns it, then shut down if unclaimed"
        return "gone", f"Likely deleted: no billing records since {last_row_date}. Confirm in the org project list and drop from tracking"
    if days_human is None or days_human > 180:
        if avg3 < idle:
            return "idle", "Shut down: no people activity and no spend"
        return "spend", "Spending with no people activity: find the owner, then shut down or move to a known team"
    if days_human > 90:
        return "review", "Low recent activity: ask the owner to confirm it is still needed"
    return "active", "In use: confirm the owner and add owner/team labels"


def run(names: list[str], progress=print, as_of: date | None = None) -> list[dict]:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import bigquery

    cfg = load_config()["gcp"]
    as_of = as_of or date.today()
    MONTHS = _months(as_of)
    since = as_of.replace(day=1) - relativedelta(months=13)           # Admin Activity logs are kept 400 days
    now = pd.Timestamp(as_of, tz="UTC")

    # ---------------------------------------------------------------- 1. billing
    client = bigquery.Client(project=cfg["project"])
    table_id = cfg["table"]
    all_projects = client.query(f"SELECT DISTINCT project.id AS pid, project.name AS pname FROM `{table_id}` WHERE project.id IS NOT NULL").to_dataframe()
    wanted = {n.lower() for n in names}
    projects = all_projects[all_projects.pname.str.lower().isin(wanted) | all_projects.pid.str.lower().isin(wanted)]
    matched = set(projects.pname.str.lower()) | set(projects.pid.str.lower())
    missing = [n for n in names if n.lower() not in matched]
    ids = sorted(projects.pid)
    progress(f"{len(names)} names -> {len(ids)} projects" + (f" | NOT FOUND in billing: {missing}" if missing else ""))
    job = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids),
                                                    bigquery.ScalarQueryParameter("m0", "STRING", MONTHS[0])])
    credits = "(SELECT IFNULL(SUM(c.amount), 0) FROM UNNEST(credits) c)"
    base = f"FROM `{table_id}` WHERE project.id IN UNNEST(@ids)"
    monthly_df = client.query(f"SELECT project.id AS pid, invoice.month AS month, SUM(cost) + SUM({credits}) AS net {base} AND invoice.month >= @m0 GROUP BY 1, 2", job_config=job).to_dataframe()
    services_df = client.query(f"SELECT project.id AS pid, service.description AS service, SUM(cost) AS gross, SUM(cost) + SUM({credits}) AS net {base} AND invoice.month >= @m0 GROUP BY 1, 2", job_config=job).to_dataframe()
    lifetime_df = client.query(f"""SELECT project.id AS pid, MAX(IF(cost > 0.001, usage_end_time, NULL)) AS last_cost,
        MAX(usage_end_time) AS last_usage_row, MIN(usage_start_time) AS first_seen, SUM(cost) AS life_gross {base} GROUP BY 1""", job_config=job).to_dataframe()
    progress(f"billing: {len(monthly_df):,} monthly rows, {len(services_df):,} service rows")

    # ---------------------------------------------------------------- 2. project details, IAM, audit logs
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)

    def call(method, url, body=None):
        for attempt in range(6):                                           # back off on HTTP 429
            r = session.request(method, url, json=body)
            if r.status_code != 429:
                return r
            time.sleep(15 * (attempt + 1))
        return r

    def read_logs(pid, flt, pages=3):
        entries, token = [], None
        for _ in range(pages):
            body = {"resourceNames": [f"projects/{pid}"], "filter": flt, "orderBy": "timestamp desc", "pageSize": 1000}
            if token:
                body["pageToken"] = token
            r = call("POST", "https://logging.googleapis.com/v2/entries:list", body)
            if not r.ok:
                return entries, r.status_code
            j = r.json()
            for e in j.get("entries", []):
                pp = e.get("protoPayload", {})
                entries.append({"ts": e["timestamp"], "log": e["logName"].split("%2F")[-1],
                                "who": pp.get("authenticationInfo", {}).get("principalEmail", ""),
                                "svc": pp.get("serviceName", ""), "method": pp.get("methodName", "")})
            token = j.get("nextPageToken")
            if not token:
                break
            time.sleep(1.1)
        return entries, None

    since_ts = f"{since.isoformat()}T00:00:00Z"
    reads_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gcp = {}
    for i, pid in enumerate(ids, 1):
        d = {}
        r = call("GET", f"https://cloudresourcemanager.googleapis.com/v3/projects/{pid}")
        d["meta"] = r.json() if r.ok else {"error": r.status_code}
        r = call("POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{pid}:getIamPolicy", {})
        d["iam"] = r.json() if r.ok else {"error": r.status_code}
        d["writes"], d["log_error"] = read_logs(pid, f'logName:"cloudaudit.googleapis.com%2Factivity" AND timestamp>="{since_ts}" AND {HUMAN}')
        d["reads"], _ = read_logs(pid, f'logName:"cloudaudit.googleapis.com%2Fdata_access" AND timestamp>="{reads_ts}" AND {HUMAN}')
        d["bots"], _ = read_logs(pid, 'logName:"cloudaudit.googleapis.com" AND protoPayload.authenticationInfo.principalEmail:"gserviceaccount.com"')
        gcp[pid] = d
        progress(f"[{i:>2}/{len(ids)}] {pid:<34} {d['meta'].get('state', d['meta'].get('error'))!s:<8} "
                 f"changes={len(d['writes']):<5} reads={len(d['reads']):<5} bot-entries={len(d['bots'])}")
        time.sleep(1.5)

    # ---------------------------------------------------------------- 3. classify
    def entry_summary(entries):
        out = {}
        for e in entries:                                                  # newest first
            u = out.setdefault(e["who"], {"who": e["who"], "last": e["ts"], "n": 0, "what": f"{e['svc']} · {e['method']}"})
            u["n"] += 1
        return out

    rows = []
    for _, lt in lifetime_df.iterrows():
        pid = lt.pid
        d = gcp.get(pid, {})
        meta, iam = d.get("meta", {}), d.get("iam", {})
        name = projects[projects.pid == pid].pname.iloc[0]
        by_month = monthly_df[monthly_df.pid == pid].set_index("month").net.to_dict()
        series = [round(float(by_month.get(m, 0) or 0), 2) for m in MONTHS]
        cost12, avg3 = sum(series[:-1]), sum(series[-4:-1]) / 3
        svc = services_df[(services_df.pid == pid) & (services_df.gross >= 0.01)].sort_values("gross", ascending=False)
        services = [{"name": s.service, "net": round(s.net, 2), "gross": round(s.gross, 2)} for s in svc.itertuples()]
        members = {}
        for b in iam.get("bindings", []):
            for m in b.get("members", []):
                members.setdefault(m, []).append(b["role"].replace("roles/", ""))
        owners = [m for m, roles in members.items() if "owner" in roles]
        writes = [e for e in d.get("writes", []) if not NOISE.search(e["method"])]
        reads = [e for e in d.get("reads", []) if not NOISE.search(e["method"]) and e["who"] != "system@google.com"]
        hands_on = sorted(writes + reads, key=lambda e: e["ts"], reverse=True)
        users = sorted(entry_summary(hands_on).values(), key=lambda u: u["last"], reverse=True)
        last_human = users[0]["last"] if users else None
        days_human = (now - pd.Timestamp(last_human)).days if last_human else None
        bots = d.get("bots", [])
        top_sa = sorted(entry_summary(bots).values(), key=lambda a: -a["n"])[:5]
        visible = "bindings" in iam or meta.get("state") == "ACTIVE"
        last_row = pd.Timestamp(lt.last_usage_row)
        status, action = classify(visible, avg3, days_human, (now - last_row).days, last_row.date())
        notes = []
        if not visible:
            notes.append("Project not readable by your account (403/404): owner/activity unknown")
        if d.get("log_error"):
            notes.append(f"Audit-log read failed (HTTP {d['log_error']}): activity may be understated")
        rows.append(dict(
            provider="GCP", name=name, id=pid, state=meta.get("state") or ("" if visible else "Not visible"), status=status, action=action,
            created=meta.get("createTime", "")[:10], parent=meta.get("parent", ""), labels=meta.get("labels", {}),
            month_labels=MONTHS, months=series, cost12=round(cost12, 2), avg3=round(avg3, 2), life=round(float(lt.life_gross), 2),
            last_billed=str(pd.Timestamp(lt.last_cost).date()) if pd.notna(lt.last_cost) else None,
            services=services, owners=owners, members=[{"m": m, "roles": r} for m, r in sorted(members.items())],
            n_access=sum(m.split(":")[0] in ("user", "group") for m in members), users=users[:25], n_users=len(users),
            last_activity=last_human, days_activity=days_human, activity_basis="Hands-on activity by a person (audit logs)",
            top_sa=top_sa, notes=notes))
    rows.sort(key=lambda x: (["spend", "idle", "gone", "review", "active"].index(x["status"]), -x["cost12"]))
    return rows
