"""AWS orphaned-account deep audit, run with the CURRENT USER's SSO / profile credentials (management account).

No AWS equivalent existed, so this mirrors the GCP audit with what AWS exposes:
  cost      the dashboard's own store (Cost Explorer pull) -- no extra API calls
  account   Organizations: status (ACTIVE / SUSPENDED / PENDING_CLOSURE), email, joined date, OU path, tags
  access    IAM Identity Center: users / groups assigned to the account, and with which permission sets
  activity  IAM `GenerateOrganizationsAccessReport`: last time ANY principal authenticated to each service in the account
Every call is optional: AccessDenied is recorded in `notes` and the status falls back to cost + owner file only.
"""
from __future__ import annotations
import time
from datetime import date

import pandas as pd
from botocore.exceptions import ClientError

from finops.audit import idle_threshold
from finops.config import load_config


def _paginate(client, op: str, key: str, **kw):
    out = []
    for page in client.get_paginator(op).paginate(**kw):
        out.extend(page.get(key, []))
    return out


def classify(org_status: str | None, in_org: bool | None, avg3: float, owner_known: bool, n_access: int | None,
             days_activity: int | None, activity_known: bool) -> tuple[str, str]:
    idle = idle_threshold()
    if org_status and org_status != "ACTIVE":
        return "gone", f"Account is {org_status}. Confirm closure is complete and drop it from tracking"
    if in_org is False:
        return "gone", ("No account with this name in the organization (left, closed — or renamed: check the account ID). Drop from tracking unless it still bills" if avg3 < idle else
                        f"No account with this name in the organization but still billing ~${avg3:,.0f}/mo (renamed? left?). Find out who pays for it")
    if activity_known:
        if days_activity is None or days_activity > 180:
            if avg3 < idle:
                return "idle", "Close the account: no authenticated activity in 180+ days and no spend (confirm with the OU owner first)"
            return "spend", ("Spending with no authenticated activity in 180+ days: find the owner, then close or move to a known team"
                             if not owner_known else "Billing with no recent activity although an owner is on file: confirm it is still needed")
        if days_activity > 90:
            return "review", "Low recent activity: ask the owner to confirm it is still needed"
        return "active", "In use: confirm the owner and add owner/team tags"
    # activity unknown (access report not available): cost + owner file only
    if avg3 < idle:
        return "idle", "No spend; activity could not be checked. Verify in the account, then close"
    if not owner_known and not (n_access or 0):
        return "spend", "Spending with no owner on file and no named access found: identify the owner (activity not checked)"
    return "active", "Billing with an owner or access on file (activity not checked)"


def run(cost: pd.DataFrame, profile: str | None = None, progress=print, as_of: date | None = None) -> list[dict]:
    """cost: AWS rows of the dashboard store (all categories, billed months)."""
    from ingest import aws as aws_ingest
    cfg = load_config()["aws"]
    as_of = as_of or date.today()
    session = aws_ingest.user_session(profile)
    notes_global: list[str] = []

    # ---- cost per account from the store -------------------------------------------------------------------
    c = cost[(cost["provider"] == "AWS") & (cost["category"] != "Tax")]
    last = c["date"].max()
    months = pd.date_range(last - pd.DateOffset(months=11), last, freq="MS")
    piv = c[c["date"].isin(months)].pivot_table(index="account", columns="date", values="cost_net", aggfunc="sum").reindex(columns=months).fillna(0.0)
    owners_file = c.groupby("account")["owner"].first().fillna("(unassigned)")
    fam = c.groupby("account")["family"].first()
    svc = (c[c["date"] > last - pd.DateOffset(months=3)].groupby(["account", "service"])["cost_net"].sum().reset_index())

    # ---- Organizations -----------------------------------------------------------------------------------
    org = session.client("organizations", region_name="us-east-1")
    accounts, org_id, root_id = {}, None, None
    try:
        org_id = org.describe_organization()["Organization"]["Id"]
        root_id = org.list_roots()["Roots"][0]["Id"]
        for a in _paginate(org, "list_accounts", "Accounts"):
            accounts[a["Name"].strip().casefold()] = a
        progress(f"organizations: {len(accounts)} accounts")
    except ClientError as e:
        notes_global.append(f"Organizations not readable ({e.response['Error']['Code']}): account status/OU unknown")
        progress(notes_global[-1])
    by_id = {a["Id"]: a for a in accounts.values()}
    ou_cache: dict[str, str] = {}

    def ou_path(acct_id: str):
        """('Root/OU/Sub', 'ou-id/ou-id') walking parents up to the root."""
        names, ids, cur = [], [], acct_id
        for _ in range(8):
            try:
                par = org.list_parents(ChildId=cur)["Parents"][0]
            except (ClientError, IndexError):
                break
            if par["Type"] == "ROOT":
                break
            if par["Id"] not in ou_cache:
                try:
                    ou_cache[par["Id"]] = org.describe_organizational_unit(OrganizationalUnitId=par["Id"])["OrganizationalUnit"]["Name"]
                except ClientError:
                    ou_cache[par["Id"]] = par["Id"]
            names.insert(0, ou_cache[par["Id"]]); ids.insert(0, par["Id"]); cur = par["Id"]
        return "/".join(names), "/".join(ids)

    # ---- IAM Identity Center ---------------------------------------------------------------------------------
    sso_region = cfg["sso_region"]
    sso = session.client("sso-admin", region_name=sso_region)
    ids_client = session.client("identitystore", region_name=sso_region)
    instance = None
    try:
        inst = sso.list_instances()["Instances"]
        instance = inst[0] if inst else None
    except ClientError as e:
        notes_global.append(f"IAM Identity Center not readable ({e.response['Error']['Code']}): access lists unavailable")
        progress(notes_global[-1])
    ps_names: dict[str, str] = {}
    principals: dict[tuple, str] = {}

    def principal_name(ptype: str, pid: str) -> str:
        key = (ptype, pid)
        if key not in principals:
            try:
                if ptype == "USER":
                    u = ids_client.describe_user(IdentityStoreId=instance["IdentityStoreId"], UserId=pid)
                    principals[key] = u.get("UserName") or u.get("DisplayName") or pid
                else:
                    g = ids_client.describe_group(IdentityStoreId=instance["IdentityStoreId"], GroupId=pid)
                    principals[key] = g.get("DisplayName") or pid
            except ClientError:
                principals[key] = pid
        return principals[key]

    def assignments(acct_id: str):
        members: dict[str, list[str]] = {}
        for ps in _paginate(sso, "list_permission_sets_provisioned_to_account", "PermissionSets",
                            InstanceArn=instance["InstanceArn"], AccountId=acct_id):
            if ps not in ps_names:
                try:
                    ps_names[ps] = sso.describe_permission_set(InstanceArn=instance["InstanceArn"], PermissionSetArn=ps)["PermissionSet"]["Name"]
                except ClientError:
                    ps_names[ps] = ps.split("/")[-1]
            for a in _paginate(sso, "list_account_assignments", "AccountAssignments", InstanceArn=instance["InstanceArn"],
                               AccountId=acct_id, PermissionSetArn=ps):
                members.setdefault(f"{a['PrincipalType'].lower()}:{principal_name(a['PrincipalType'], a['PrincipalId'])}", []).append(ps_names[ps])
        return members

    # ---- IAM organizations access report (last authenticated use per service) --------------------------------
    iam = session.client("iam", region_name="us-east-1")

    def access_report(entity_path: str):
        for attempt in range(5):
            try:
                job = iam.generate_organizations_access_report(EntityPath=entity_path)["JobId"]
                break
            except ClientError as e:
                if e.response["Error"]["Code"] in ("ReportGenerationLimitExceeded", "Throttling", "ThrottlingException"):
                    time.sleep(10 * (attempt + 1)); continue
                raise
        else:
            raise RuntimeError("access report throttled")
        details = []
        for _ in range(40):
            r = iam.get_organizations_access_report(JobId=job, MaxItems=1000)
            if r["JobStatus"] == "IN_PROGRESS":
                time.sleep(2); continue
            if r["JobStatus"] == "FAILED":
                raise RuntimeError(r.get("ErrorDetails", {}).get("Message", "report failed"))
            details.extend(r.get("AccessDetails", []))
            while r.get("IsTruncated"):
                r = iam.get_organizations_access_report(JobId=job, MaxItems=1000, Marker=r["Marker"]); details.extend(r.get("AccessDetails", []))
            return details
        raise RuntimeError("access report timed out")

    activity_denied = False
    rows = []
    names = list(piv.index)
    for i, name in enumerate(names, 1):
        series = [round(float(v), 2) for v in piv.loc[name].values]
        cost12, avg3 = sum(series), sum(series[-3:]) / 3
        acc = accounts.get(str(name).strip().casefold())        # the store keeps names, not IDs: match case-insensitively
        acct_id = acc["Id"] if acc else (name if name.isdigit() and len(name) == 12 else None)
        notes = list(notes_global)
        in_org = (acc is not None) if accounts else None
        parent, parent_ids = ("", "")
        tags = {}
        if acc:
            parent, parent_ids = ou_path(acc["Id"])
            try:
                tags = {t["Key"]: t["Value"] for t in _paginate(org, "list_tags_for_resource", "Tags", ResourceId=acc["Id"])}
            except ClientError:
                pass
        members = {}
        if instance and acct_id:
            try:
                members = assignments(acct_id)
            except ClientError as e:
                notes.append(f"Identity Center assignments unreadable ({e.response['Error']['Code']})")
        last_auth, activity_known, top_services = None, False, []
        if acc and org_id and not activity_denied:
            try:
                det = access_report("/".join(p for p in (org_id, root_id, parent_ids, acc["Id"]) if p))
                times = [(d.get("LastAuthenticatedTime"), d.get("ServiceName") or d.get("ServiceNamespace")) for d in det if d.get("LastAuthenticatedTime")]
                activity_known = True
                if times:
                    last_auth = max(t for t, _ in times)
                    top_services = [s for _, s in sorted(times, reverse=True)[:5]]
            except Exception as e:
                code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
                notes.append(f"Access report unavailable ({code}): last-activity not checked")
                if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
                    activity_denied = True
        days = (pd.Timestamp(as_of, tz="UTC") - pd.Timestamp(last_auth).tz_convert("UTC")).days if last_auth else None
        owner_known = owners_file.get(name, "(unassigned)") != "(unassigned)"
        n_access = len(members) if instance else None
        status, action = classify(acc["Status"] if acc else None, in_org, avg3, owner_known, n_access, days, activity_known)
        sv = svc[svc.account == name].sort_values("cost_net", ascending=False)
        owners = [m for m, ps in members.items() if any("admin" in p.lower() or "owner" in p.lower() for p in ps)]
        rows.append(dict(
            provider="AWS", name=name, id=acct_id or "", state=acc["Status"] if acc else ("Not in organization" if accounts else ""),
            status=status, action=action, created=str(acc["JoinedTimestamp"])[:10] if acc else "", parent=parent or "(root)" if acc else "",
            labels=tags, month_labels=[m.strftime("%Y%m") for m in months], months=series, cost12=round(cost12, 2), avg3=round(avg3, 2),
            life=round(cost12, 2), last_billed=None, services=[{"name": r.service, "net": round(r.cost_net, 2), "gross": round(r.cost_net, 2)} for r in sv.head(8).itertuples()],
            owners=owners, members=[{"m": m, "roles": r} for m, r in sorted(members.items())], n_access=n_access, users=[], n_users=0,
            last_activity=str(last_auth) if last_auth else None, days_activity=days,
            activity_basis="Last authenticated service use by any principal (IAM organizations access report)",
            top_sa=[{"who": s, "last": "", "n": 0, "what": ""} for s in top_services], notes=sorted(set(notes)),
            owner_on_file=owners_file.get(name, "(unassigned)"), family=fam.get(name, "")))
        progress(f"[{i:>2}/{len(names)}] {name[:34]:<34} {rows[-1]['state'][:10]:<10} avg3=${avg3:>9,.0f} access={n_access} "
                 f"last_auth={str(last_auth)[:10] if last_auth else '—'} -> {status}")
    rows.sort(key=lambda x: (["spend", "idle", "gone", "review", "active"].index(x["status"]), -x["cost12"]))
    return rows
