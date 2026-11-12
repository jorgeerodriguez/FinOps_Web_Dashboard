"""AWS ingest: Cost Explorer, run with the CURRENT USER's own credentials.

Ported (and trimmed) from AWS_FinOps_By_Account.py: SSO device login, roll-up role, linked-account x service
monthly cost, Marketplace/Tax classification. Credentials are never stored by this project.
"""
from __future__ import annotations
import os
import webbrowser
from datetime import date
from time import sleep

import pandas as pd
from dateutil.relativedelta import relativedelta

from finops.config import load_config
from finops.schema import CLOUD, MARKETPLACE, TAX

METRIC = "NetUnblendedCost"


def user_session(profile: str | None = None):
    """boto3 Session for the roll-up account. Uses --profile / AWS_PROFILE if given, else SSO device login."""
    from boto3.session import Session
    cfg = load_config()["aws"]
    profile = profile or os.environ.get("AWS_PROFILE")
    if profile:
        print(f"Using AWS profile '{profile}' (run `aws sso login --profile {profile}` first if expired)")
        return Session(profile_name=profile, region_name="us-east-1")

    region, start_url, account = cfg["sso_region"], cfg["sso_start_url"], cfg["rollup_account_id"]
    session = Session()
    oidc = session.client("sso-oidc", region_name=region)
    client = oidc.register_client(clientName="finops-dashboard", clientType="public")
    auth = oidc.start_device_authorization(clientId=client["clientId"], clientSecret=client["clientSecret"],
                                           startUrl=start_url)
    print("Approve the AWS sign-in in your browser:", auth["verificationUriComplete"], flush=True)
    webbrowser.open(auth["verificationUriComplete"], autoraise=True)
    token = None
    for _ in range(auth["expiresIn"] // auth["interval"] + 1):
        sleep(auth["interval"])
        try:
            token = oidc.create_token(grantType="urn:ietf:params:oauth:grant-type:device_code",
                                      deviceCode=auth["deviceCode"], clientId=client["clientId"],
                                      clientSecret=client["clientSecret"])
            break
        except oidc.exceptions.AuthorizationPendingException:
            continue
        except oidc.exceptions.SlowDownException:
            sleep(auth["interval"])
    if token is None:
        raise TimeoutError("AWS SSO login was not approved in time")
    sso = session.client("sso", region_name=region)
    roles = [r["roleName"] for r in sso.list_account_roles(accessToken=token["accessToken"], accountId=account)["roleList"]]
    rank = lambda r: 0 if "ReadOnly" in r and "Billing" in r else 1 if "ReadOnly" in r else 2 if "Billing" in r else 3
    role = sorted(roles, key=rank)[0]
    creds = sso.get_role_credentials(roleName=role, accountId=account, accessToken=token["accessToken"])["roleCredentials"]
    print(f"Using role {role} on {account}")
    return Session(region_name="us-east-1", aws_access_key_id=creds["accessKeyId"],
                   aws_secret_access_key=creds["secretAccessKey"], aws_session_token=creds["sessionToken"])


def _query(ce, start: str, end: str, group_by: list[str]):
    rows, names, token = [], {}, None
    while True:
        kw = dict(TimePeriod={"Start": start, "End": end}, Granularity="MONTHLY", Metrics=[METRIC],
                  GroupBy=[{"Type": "DIMENSION", "Key": k} for k in group_by])
        if token:
            kw["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kw)
        for a in resp.get("DimensionValueAttributes", []):
            names[a["Value"]] = a.get("Attributes", {}).get("description", "")
        for r in resp["ResultsByTime"]:
            for g in r["Groups"]:
                rows.append((r["TimePeriod"]["Start"], *g["Keys"], float(g["Metrics"][METRIC]["Amount"])))
        token = resp.get("NextPageToken")
        if not token:
            return rows, names


# Same fallback rule as your classify_service (used only if a service has no billing-entity answer)
_AWS_OVERRIDES = {"EC2 - Other", "Savings Plans for AWS Compute usage", "AmazonCloudWatch", "AmazonWorkMail",
                  "CloudWatch Events", "CodeBuild", "CodeCatalyst", "AWSDevOpsAgent", "Refund", "OCBCloudFront",
                  "OCBLateFee", "OCBFinance Charge", "Training and Certification Vouchers",
                  "Training Certification Vouchers - backup", "Training Certification Vouchers - backup2"}


def _by_name(service: str) -> str:
    if "Amazon Bedrock Edition" in service or service == "Claude Platform":
        return MARKETPLACE                      # third-party Bedrock models bill via Marketplace
    return CLOUD if service.startswith(("AWS", "Amazon")) or service in _AWS_OVERRIDES else MARKETPLACE


def fetch(session, months: int) -> pd.DataFrame:
    """Normalized rows for the last `months` months including the in-progress one (is_partial)."""
    ce = session.client("ce", region_name="us-east-1")
    first_this = date.today().replace(day=1)
    start = first_this - relativedelta(months=months - 1)
    end = date.today() + relativedelta(days=1)                  # End is exclusive
    s, e = start.isoformat(), end.isoformat()
    acct_rows, names = _query(ce, s, e, ["LINKED_ACCOUNT", "SERVICE"])
    ent_rows, _ = _query(ce, s, e, ["SERVICE", "BILLING_ENTITY"])

    df = pd.DataFrame(acct_rows, columns=["date", "account_id", "service", "cost"])
    ent = pd.DataFrame(ent_rows, columns=["date", "service", "entity", "cost"])
    entity = (ent.groupby(["service", "entity"])["cost"].sum().abs().reset_index().sort_values("cost")
              .drop_duplicates("service", keep="last").set_index("service")["entity"])
    df["date"] = pd.to_datetime(df["date"])
    ent_of = df["service"].map(entity)
    df["category"] = [TAX if sv == "Tax" else (MARKETPLACE if "Marketplace" in en else CLOUD) if isinstance(en, str)
                      else _by_name(sv) for sv, en in zip(df["service"], ent_of)]
    df["account"] = df["account_id"].map(names).replace("", pd.NA).fillna(df["account_id"])
    return pd.DataFrame({
        "date": df["date"], "provider": "AWS", "account": df["account"], "service": df["service"],
        "category": df["category"], "env": "unknown",
        "cost_gross": df["cost"], "credits": 0.0, "cost_net": df["cost"],
        "is_partial": df["date"] >= pd.Timestamp(first_this),
    })


# ----------------------------------------------------------------------------- cost allocation tags
def _known_tags(ce) -> list[dict]:
    out, token = [], None
    while True:
        r = ce.list_cost_allocation_tags(**({"NextToken": token} if token else {}))
        out += r["CostAllocationTags"]
        token = r.get("NextToken")
        if not token:
            return out


def _tag_query(ce, start: str, end: str, key: str) -> list[tuple]:
    """(account_id, value, cost) summed over the window, for one tag key. Untagged spend comes back as an empty value and is skipped."""
    sums, token = {}, None
    while True:
        kw = dict(TimePeriod={"Start": start, "End": end}, Granularity="MONTHLY", Metrics=[METRIC],
                  GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}, {"Type": "TAG", "Key": key}])
        if token:
            kw["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kw)
        for r in resp["ResultsByTime"]:
            for g in r["Groups"]:
                acct, tagv = g["Keys"][0], g["Keys"][1]
                value = tagv.split("$", 1)[1] if "$" in tagv else tagv
                sums[(acct, value)] = sums.get((acct, value), 0.0) + float(g["Metrics"][METRIC]["Amount"])
        token = resp.get("NextPageToken")
        if not token:
            return [(a, v, c) for (a, v), c in sums.items() if v]


def fetch_tags(session, months: int | None = None):
    """(accounts, values, catalog) tables for finops.tags.save_provider. Read-only calls; Cost Explorer bills about $0.01 per request."""
    from finops import tags as T
    cfg = T.tag_config()
    months = months or cfg["months"]
    ce = session.client("ce", region_name="us-east-1")
    first_this = date.today().replace(day=1)
    s, e = (first_this - relativedelta(months=months)).isoformat(), first_this.isoformat()      # whole billed months only
    window = f"{s[:7]}..{(first_this - relativedelta(months=1)).isoformat()[:7]}"
    known = _known_tags(ce)
    keys, why = T.aws_keys(known, cfg)
    totals, names = _query(ce, s, e, ["LINKED_ACCOUNT"])
    tot = pd.DataFrame(totals, columns=["date", "account_id", "total"]).groupby("account_id", as_index=False)["total"].sum()
    tot["account"] = tot["account_id"].map(names).replace("", pd.NA).fillna(tot["account_id"])
    parts = []
    for i, k in enumerate(keys, 1):
        rows = _tag_query(ce, s, e, k)
        print(f"  tag {i}/{len(keys)}: {k!r} -> {len(rows)} account x value rows", flush=True)
        parts.append(pd.DataFrame(rows, columns=["account_id", "value", "cost"]).assign(key=k))
    tr = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["account_id", "value", "cost", "key"])
    tr["account"] = tr["account_id"].map(dict(zip(tot["account_id"], tot["account"])))
    status = {t["TagKey"]: ("Active" if t["Status"] == "Active" else "Inactive") for t in known}
    for k in why:
        status.setdefault(k, "not found")
    return T.build("AWS", tot[["account_id", "account", "total"]], tr[["key", "account_id", "account", "value", "cost"]], status, window, cfg, why)
