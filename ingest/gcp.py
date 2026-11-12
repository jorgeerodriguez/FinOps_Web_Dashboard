"""GCP ingest: BigQuery billing export, run with the CURRENT USER's Application Default Credentials.

Query ported from multicloud_kpis.refresh_gcp_history. Credentials: `gcloud auth application-default login`.
"""
from __future__ import annotations
import pandas as pd
from dateutil.relativedelta import relativedelta

from finops.config import load_config
from finops.schema import CLOUD, MARKETPLACE, TAX


def check_credentials() -> str | None:
    """Return None if ADC works, else a human-readable instruction."""
    try:
        import google.auth
        google.auth.default()
        return None
    except Exception as e:
        return ("No Google credentials found for your user. Run:\n"
                "    gcloud auth application-default login\n"
                f"({type(e).__name__})")


def fetch(months: int) -> pd.DataFrame:
    from google.cloud import bigquery
    cfg = load_config()["gcp"]
    first_this = pd.Timestamp.today().normalize().replace(day=1)
    start = first_this - relativedelta(months=months - 1)
    m0, m1 = start.strftime("%Y%m"), first_this.strftime("%Y%m")
    sql = f"""
    SELECT invoice.month AS invoice_month, service.description AS service,
           IFNULL(project.name, '(no project)') AS project,
           IFNULL(transaction_type, 'GOOGLE') AS transaction_type, IFNULL(cost_type, 'regular') AS cost_type,
           EXISTS(SELECT 1 FROM UNNEST(labels) l WHERE LOWER(l.key) IN ('env', 'environment')) AS has_env_label,
           SUM(cost) AS cost,
           SUM(IFNULL((SELECT SUM(x.amount) FROM UNNEST(credits) x), 0)) AS credits
    FROM `{cfg['table']}`
    WHERE invoice.month BETWEEN '{m0}' AND '{m1}'
    GROUP BY 1, 2, 3, 4, 5, 6"""
    h = bigquery.Client(project=cfg["project"]).query(sql).to_dataframe()
    cat = pd.Series(CLOUD, index=h.index)
    cat[h["transaction_type"].ne("GOOGLE")] = MARKETPLACE          # resellers / agencies
    cat[h["cost_type"].eq("tax")] = TAX
    date = pd.to_datetime(h["invoice_month"], format="%Y%m")
    return pd.DataFrame({
        "date": date, "provider": "GCP", "account": h["project"], "service": h["service"], "category": cat,
        "env": h["has_env_label"].map({True: "labeled", False: "untagged"}),
        "cost_gross": h["cost"], "credits": h["credits"], "cost_net": h["cost"] + h["credits"],
        "is_partial": date >= first_this,
    })


def fetch_tags(months: int | None = None):
    """(accounts, values, catalog) tables for finops.tags.save_provider. Read-only BigQuery queries on the billing export (they scan roughly
    10-15 GB each, a few cents). Project labels win over resource labels. Only regular cost (no tax / adjustments)."""
    from google.cloud import bigquery
    from finops import tags as T
    cfg = T.tag_config()
    months = months or cfg["months"]
    g = load_config()["gcp"]
    first_this = pd.Timestamp.today().normalize().replace(day=1)
    m0, m1 = (first_this - relativedelta(months=months)).strftime("%Y%m"), (first_this - relativedelta(months=1)).strftime("%Y%m")
    src = f"`{g['table']}`"
    where = f"WHERE invoice.month BETWEEN '{m0}' AND '{m1}' AND IFNULL(cost_type, 'regular') = 'regular'"
    client = bigquery.Client(project=g["project"])
    tot = client.query(f"SELECT IFNULL(project.id, '') AS account_id, IFNULL(project.name, '(no project)') AS account, SUM(cost) AS total FROM {src} {where} GROUP BY 1, 2").to_dataframe()
    raw = client.query(f"""
        SELECT IFNULL(project.id, '') AS account_id, IFNULL(project.name, '(no project)') AS account, l.key AS key, l.value AS value, l.src AS src, SUM(cost) AS cost
        FROM {src}, UNNEST(ARRAY_CONCAT(ARRAY(SELECT AS STRUCT p.key AS key, p.value AS value, 'project' AS src FROM UNNEST(project.labels) p),
                                    ARRAY(SELECT AS STRUCT r.key AS key, r.value AS value, 'resource' AS src FROM UNNEST(labels) r))) AS l
        {where}
        GROUP BY 1, 2, 3, 4, 5""").to_dataframe()
    for c in ("total",):
        tot[c] = tot[c].astype(float)
    raw["cost"] = raw["cost"].astype(float)
    rows, status, why = T.gcp_rows(raw, cfg)
    return T.build("GCP", tot, rows, status, f"{m0[:4]}-{m0[4:]}..{m1[:4]}-{m1[4:]}", cfg, why)
