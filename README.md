# FinOps Web Dashboard

A self-contained [Streamlit](https://streamlit.io) dashboard for monthly **AWS + GCP FinOps reporting**: executive summary,
budget tracking, forecasting (current year and next-year budget), AWS-vs-GCP comparison, Marketplace contract calendar,
anomaly review, orphaned account/project audits, a month-to-date view, and print-ready HTML reports.

> **Public repository — read this first.** This repo contains **code only**. Your billing data, owner lists, audit results,
> generated reports and environment config (`config.toml`) are git-ignored and must never be committed.
> Start from `config.example.toml` and the files in `examples/`.

## Highlights

| Page | What it answers |
|---|---|
| **Executive Summary** | KPIs, monthly spend vs budget, cumulative spend vs budget, where the money goes (service / account / project) with year-over-year on overlapping months, Marketplace & Tax reported separately |
| **Month to Date** | In-progress month (as-now): projected month-end vs budget, lumps handled line by line, Marketplace charges still scheduled |
| **AWS / GCP** | Total, by account/project, by service, owner or family; per-entity forecast and billing detail |
| **AWS vs GCP** | Trend, share and annual totals on comparable months |
| **Forecast & Budget** | Current-year and next-year forecasts with a what-if adjustment |
| **Next Year Forecast** | Low/Base/High budget forecast per provider, bottom-up by account/project, quarterly & monthly budget tables, reconciliation, growth drivers, anomaly review |
| **Marketplace** | Contract renewal calendar (billed vs scheduled), quarterly commitments, renewal watch |
| **Relationships** | Sankey (owner → account/project → service) and concentration matrix |
| **Insights & Savings** | Spikes, new spend, creeping growth, ownership gaps, cost-structure checks |
| **Orphaned Accounts & Projects** | Cost-based screening plus an optional deep audit (access and activity) for AWS and GCP |
| **Data Health** | Refresh buttons (your credentials), refresh history, data gaps |

Every page that matters for leadership has a **Build HTML report** button: self-contained, light theme, landscape Letter,
print to PDF from any browser (Executive Summary, Next Year Forecast, Marketplace calendar).

## Design principles

- **Spend categories are never mixed.** Every row is *Cloud Services* (budget basis), *Marketplace* or *Tax*. Budgets apply to Cloud Services only.
- **Independent of other folders.** The dashboard owns its data store (`data/billing.parquet`); pages read only that.
- **Credentials belong to the person running it.** AWS SSO / profile and GCP Application Default Credentials are used at refresh time and never stored.
- **Explainable forecasting.** Run-rate + robust trend (Theil-Sen), Low/Base/High scenarios, backtested model choice per account/project. No black boxes.
- **Auditable adjustments.** Billing gaps are reviewed and recorded in a CSV, not silently patched in code.
- **Dark mode follows the system setting** (Streamlit Settings menu); reports are always light.

## Quick start

Requires Python 3.11+.

```bash
git clone https://github.com/jorgeerodriguez/FinOps_Web_Dashboard.git
cd FinOps_Web_Dashboard

# keep the virtualenv outside synced folders (e.g. Google Drive)
python3 -m venv ~/.venvs/finops_dash
~/.venvs/finops_dash/bin/pip install -r requirements.txt

cp config.example.toml config.toml        # then edit: SSO URL, payer account, GCP billing table, budgets
mkdir -p data
cp examples/owners_aws.example.csv data/owners_aws.csv     # optional: owner/family mapping
cp examples/owners_gcp.example.csv data/owners_gcp.csv

~/.venvs/finops_dash/bin/streamlit run app.py              # http://localhost:8502
```

On first run the dashboard is empty. Open **Data Health** and click **Refresh AWS** / **Refresh GCP**, or use the CLI:

```bash
# AWS: Cost Explorer via your SSO login (browser device flow) or an existing profile
python -m ingest.run --provider aws [--profile NAME]

# GCP: BigQuery billing export via your Application Default Credentials
gcloud auth application-default login
python -m ingest.run --provider gcp

python -m ingest.run --provider all      # both; default window = 14 months incl. the in-progress month
```

## Data and configuration

`config.toml` (git-ignored, copy from `config.example.toml`):

| Section | Purpose |
|---|---|
| `[budget.<year>]` | Monthly Cloud Services budget per provider (`aws_monthly`, `gcp_monthly`) |
| `[forecast]` | YoY window, default band, `adjustments_csv`; `[forecast.trend_since]` to measure a provider's trend from a given month (skips a launch ramp) |
| `[data]` | Data folder and refresh window (`history_months`, 14 by default — Cost Explorer's default retention) |
| `[aws]`, `[gcp]` | SSO start URL, payer (management) account, GCP billing project and BigQuery export table |
| `[owners]` | Paths to `owners_aws.csv` / `owners_gcp.csv` |
| `[audit]` | Audit folder, GCP project list, idle threshold, notes to hide |

Files under `data/` (all git-ignored; see `examples/` for the shape):

| File | Purpose |
|---|---|
| `billing.parquet` | Normalized store: date, provider, account, service, category, owner, family, gross/credits/net, `is_partial` |
| `refresh_log.csv` | Who refreshed what, and when |
| `owners_aws.csv`, `owners_gcp.csv` | Owner / family / department per account or project (edit freely; **Reload data** after) |
| `anomaly_rules.csv` | Reviewed billing gaps: `add_back` · `confirmed_real` · `pending`, with a note |
| `orphans_gcp.txt` | GCP projects (names or IDs) for the deep audit |
| `audits/` | Saved deep-audit results |

## How it works

```
app.py                  navigation; global "spend category" selector
views/                  one file per page (thin: layout only)
finops/
  schema.py store.py data.py owners.py     normalized schema, parquet store, loading
  forecast.py                              current-year forecast (YoY ratio / run-rate, backtested)
  nextyear.py nextyear_summary.py          next-year Low/Base/High, per-line model competition, anomaly rules
  marketplace.py marketplace_view.py       contract cadence -> renewal calendar
  asnow.py                                 month-to-date projection
  audit.py insights.py                     orphan screening, savings signals
  exec_summary.py exec_charts.py ...       numbers/figures with NO Streamlit in them
  report.py *_report.py                    self-contained HTML report framework + reports
  ui.py                                    theme, money formatting, shared widgets
ingest/                 refresh (AWS Cost Explorer, GCP BigQuery) and deep audits — run with YOUR credentials
examples/               anonymized samples of the git-ignored files
```

Pages and reports share the same builders (functions that return numbers, tables and figures without Streamlit), so a page and its
HTML report cannot disagree.

### Forecasting in one paragraph
Per provider: start at the exit run-rate (last 3 billed months) and add a Theil-Sen trend measured over the last 12 billed months.
*Low* = flat, *Base* = damped trend (15% momentum loss per month), *High* = trend continues. Each account/project also competes
Flat / Damped / Linear / Holt-ETS on a backtest (capped at 2×, floored at 0.5× of the exit run-rate). Marketplace is a contract calendar,
not a trend. With only ~14 months of history, seasonality is not modelled — treat ranges accordingly.

### Month to Date
The in-progress month is stored as `is_partial` and never enters billed views, forecasts or reports. Its projection is line-by-line:
lumps already billed are not extrapolated, charges not yet posted are assumed equal to last month, and lines running far ahead of pace
are capped. The band narrows as the month progresses.

## Deep audits (optional)

```bash
python -m ingest.audit --provider gcp     # ~15-20 min for ~30 projects; needs gcloud ADC
python -m ingest.audit --provider aws     # run against the management account; Organizations + Identity Center + IAM access report
```

Calls your role cannot make are noted on the page and skipped; statuses fall back to cost and the owner file.

## Security and privacy

- Billing data, owner names, IAM membership and account IDs are sensitive. They stay in git-ignored `data/`, `reports/` and `config.toml`.
- The app never stores credentials. AWS SSO tokens and GCP ADC live where those tools normally keep them.
- Hosting it for others? Add authentication in front (SSO reverse proxy or Streamlit's OIDC) and decide who may trigger a refresh —
  a refresh uses the credentials of whoever runs the server process.

## Known limitations

- ~14 months of history (Cost Explorer's default); year-over-year comparisons cover only overlapping months.
- Month-to-date is monthly-grain (no daily burn chart yet); early-month projections are low confidence.
- AWS deep audit depends on Organizations / IAM trusted-access permissions; it degrades gracefully when denied.
- Excel can turn 12-digit account IDs into scientific notation when editing the owner CSVs. Matching also uses account names, so this
  is harmless, but format the ID column as text.

## Roadmap

Daily-grain as-now data (burn chart), savings-opportunity detectors (idle resources, commitment coverage), authentication for shared hosting,
and a scheduled monthly report.
