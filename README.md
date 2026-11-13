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
| **Executive Summary** | KPIs, monthly spend vs budget, cumulative spend vs budget, where the money goes (service / account / project) with last year's cumulative line and year-over-year on matching months, Marketplace & Tax reported separately, and **spend by project code** across AWS + GCP (projects on both clouds, environment mix, Finance readiness) |
| **Two-Year History** | Latest 12 months vs the 12 before, same month year over year, annual run-rate, what explains the change (by service / account / project), one-off months with their driver, Marketplace & Tax separately, and a look-back test of the forecast method |
| **Month to Date** | In-progress month (as-now): projected month-end vs budget, lumps handled line by line, Marketplace charges still scheduled |
| **AWS / GCP** | Total, by account/project, by service, owner, family, **project code, environment** and cost center / service name; **Projects & environments** section (project-code trend and YoY, prod vs non-prod, treemap, finance notes) with its own HTML report; per-entity forecast and billing detail |
| **AWS vs GCP** | Trend, share and annual totals on comparable months |
| **Forecast & Budget** | Current-year and next-year forecasts with a what-if adjustment |
| **Next Year Forecast** | Low/Base/High budget forecast per provider, bottom-up by account/project, quarterly & monthly budget tables, reconciliation, growth drivers, anomaly review with an in-app **rules editor**, look-back of last year's forecast |
| **Marketplace** | Contract renewal calendar (billed vs scheduled), quarterly commitments, price change vs the prior 12 months, renewal watch |
| **Relationships** | Sankey (owner → account/project → service) and concentration matrix |
| **Insights & Savings** | **Savings ledger** (sustained step-downs vs each line's own peak, realized to date, shutdowns, savings that came back, one-offs excluded) on an account, service or **project-code** lens, with where the savings landed by project code / environment / cost center; **scorecard** by owner, project code or cost center with a per-group review and a printable **review pack**; **opportunities** (non-prod as big as prod, steady always-on lines, credits); spikes, new spend and creeping growth with finance notes; ownership and **Finance gaps** |
| **Orphaned Accounts & Projects** | Cost-based screening plus an optional deep audit (access and activity) for AWS and GCP |
| **Data Health** | Refresh buttons (your credentials), refresh history, data gaps, an **owners editor** (assign owners and Finance codes to accounts / projects, including ones billing but missing from the file) with the **cloud tags** AWS and GCP already carry shown beside each row, tag coverage, and each account's lifecycle **status** (New · Active · Idle · Deleted · Orphan · Remove) |

Every page that matters for leadership has a **Build HTML report** button: self-contained, light theme, landscape Letter,
print to PDF from any browser (Executive Summary, Two-Year History, Next Year Forecast, Marketplace calendar).

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

python -m ingest.run --provider all      # both; default window = `history_months` (27) incl. the in-progress month
python -m ingest.run --provider gcp --tags-only   # only the cloud tag / label tables (no billing pull); --no-tags skips them
```

## Data and configuration

`config.toml` (git-ignored, copy from `config.example.toml`):

| Section | Purpose |
|---|---|
| `[budget.<year>]` | Monthly Cloud Services budget per provider (`aws_monthly`, `gcp_monthly`) |
| `[forecast]` | YoY window, default band, `adjustments_csv`; `[forecast.trend_since]` to measure a provider's trend from a given month (skips a launch ramp) |
| `[data]` | Data folder and refresh window (`history_months`, 27 by default = two full years plus the current one. AWS Cost Explorer keeps only ~14 months unless *multi-year data at monthly granularity* is enabled in Cost Management preferences; GCP goes back as far as the billing export was on) |
| `[aws]`, `[gcp]` | SSO start URL, payer (management) account, GCP billing project and BigQuery export table |
| `[owners]` | Paths to `owners_aws.csv` / `owners_gcp.csv` |
| `[dimensions]` | `shared_codes`: project codes that are enterprise-wide buckets (support contracts, commitment fees), left out of the project charts but kept in tables; `placeholders`: owner-file values treated as "not filled in" |
| `[status]` | `quiet_months` (default 6): an account billed before but silent this long is suggested *Deleted* |
| `[tags]` | Which cloud tags / labels to pull (`aws = "active"` or a list, `gcp = "auto"` or a list, `months`, `exclude` patterns) and `[tags.map]`: which tag key feeds `cost_center` / `project_code` |
| `[audit]` | Audit folder, GCP project list, idle threshold, notes to hide |

Files under `data/` (all git-ignored; see `examples/` for the shape):

| File | Purpose |
|---|---|
| `billing.parquet` | Normalized store: date, provider, account, service, category, owner, family, gross/credits/net, `is_partial` |
| `refresh_log.csv` | Who refreshed what, and when |
| `owners_aws.csv`, `owners_gcp.csv` | Owner / family / department per account or project, plus the Finance columns `cost_center`, `project_code`, `treatment` (Opex · Capex-eligible · Prepaid) and `finance_notes`. Edit in **Data Health** or in any spreadsheet |
| `status_log.csv` | Every status change (automatic or by hand): when, who, account, old → new, reason. `archive/` holds rows removed by a purge |
| `tags_accounts.csv`, `tags_values.csv`, `tags_catalog.csv` | Read-only cloud tag snapshot per account / project (last 3 billed months), refreshed with the tag buttons. Never overwrites what you typed in the owner files |
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
  history.py history_report.py             two-year comparison, one-off detection, forecast look-back, 'what moved' summary
  savings.py                               savings ledger (billing gaps added back), erosion, owner scorecard
  editors.py editor_ui.py                  safe in-app editing of the owner files and anomaly rules (backup, atomic save, stale-file check)
  dims.py dims_ui.py dims_report.py        project code / environment / cost center tables, charts, findings, page blocks and the Projects & Environments report
  tags.py                                  cloud tags / labels per account: key selection, dominant value + coverage, comparison with the owner file
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
not a trend. The model uses the latest 12 months; the earlier history is context and powers a look-back test (the method re-run as of 12 months ago, compared with what was billed). Seasonality is still not modelled — two years cannot separate it from growth.

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

## Tests and dependencies

```bash
pip install -r requirements-dev.txt     # app + pytest
python -m pytest                        # ~8 s: unit tests of the calculation layer + a smoke test of every page and report
```

- Tests run on **synthetic data only** (`tests/synth.py`), through `FINOPS_CONFIG`, so they never read your `config.toml` or `data/`.
  They cover the savings ledger, history/one-off detection, forecasting, the Marketplace contract calendar, the project / environment tables and reports, the cloud-tag tables and editors, the month-to-date
  projection, the store, every page (with data and on a brand-new empty install) and every HTML report. A GitHub Action runs them on each push.
- `requirements.txt` holds compatible ranges (the major versions tested); `requirements.lock` holds the exact versions
  (`pip install -r requirements.lock` for a reproducible install). Raise a cap only after the tests pass on the new major.

### Project codes, environments and cost centers

`environment`, `project_code`, `cost_center`, `service_name` and `finance_notes` from the owner files are joined onto every billing row at read time, so any page or report can group by them.
Values are tidied on the way in: spacing and case are ignored (the most common spelling wins, **across both clouds**, so `Checkout` on AWS and `CHECKOUT` on GCP are one project),
blanks and placeholders (`TBD`, `placeholder` ...) count as not filled in, and `nonprod` / `Non Prod` become `non-prod`. The provider pages, the Executive Summary and the
*Projects & Environments* HTML report show project-code trends with year-over-year change, prod vs non-prod (and where non-prod is growing), projects that bill on both clouds,
and a Finance-readiness table (share of spend that carries each code). The treemap gives every project code its own color (lighter tiles = its AWS accounts, lighter still = GCP) and folds small accounts into one tile so labels stay readable in light and dark mode. The owners editor in Data Health can show only the rows still missing an owner, a project code or an environment.

### Insights with project codes

The **project-code lens** treats everything with the same code as one line across AWS and GCP, so spend moved between a project's accounts (or between clouds) nets out instead of
reading as a saving on one line and new spend on another. The ledger, scorecard and review pack can be read per owner, project code or cost center; the *Opportunities* tab lists
**leads, not savings** (billing data cannot see utilization or existing reservations): project codes whose non-prod spend is at least their prod spend, lines that billed steadily
for 12 months (commitment candidates, to be checked against existing coverage), and credit trends. *Finance gaps* lists the biggest spend still missing an environment,
project code or cost center.

### Account status

Each row of the owner files has a `status`, right after its name column: **New** (found in billing and not seen before), **Active** (billing is current), **Idle** (never billed in the history we hold),
**Deleted** (billed before, then silent for `quiet_months`, or the deep audit says the provider removed it), **Orphan** (looks abandoned or unowned, set by you, e.g. from the Orphans page) and **Remove**
(to be purged). The file keeps the status *you* decided; the app shows a **suggested** status beside it from billing and the latest deep audit, and never changes Orphan or Remove by itself.
A refresh adds every new account to the owner file as *New* so you can fill its fields (it becomes *Active* when owner, project code and environment are filled). In Data Health → Owners you can
change a status by hand, apply the suggestions to the rows on screen, add accounts that bill but are not in the file, and **purge** rows marked Remove (they are archived to `data/archive/`, billing history is untouched).
Deleted and Remove rows drop out of the "still missing" counts, and Orphan / Deleted accounts that still bill show up on the Opportunities tab.

### Cloud tags and Finance codes

Data Health shows what AWS (cost allocation tags) and GCP (project and resource labels) already say about each account / project, read-only,
next to the columns you type. A **Tag check** column flags rows where your `cost_center` / `project_code` is empty or differs from the cloud tag,
and an optional pre-fill copies the cloud value into *empty* cells only (nothing is saved until you click Save). Coverage per Finance field and a
catalog of every tag key found are below the editor. Notes:

- AWS can only report a tag that is **active** in Billing → Cost allocation tags (activation is not retroactive). Inactive keys are listed in the catalog with that reason.
- AWS tags sit on resources, so an account can have several values: the table shows the one with the most spend, `+N` when more exist and `· 13%` when only part of the spend carries the tag.
- GCP uses the project label, falling back to resource labels. Reseller / Marketplace spend carries no labels, so it lowers coverage.
- Tag values can be Finance codes: they stay in the git-ignored `data/` folder. `treatment` is a note for Finance, not used in any calculation yet.

## Known limitations

- History is whatever the sources keep: ~27 months here (AWS needs multi-year monthly data enabled for more than ~14). Year-over-year comparisons cover the months present in both years.
- Month-to-date is monthly-grain (no daily burn chart yet); early-month projections are low confidence.
- AWS deep audit depends on Organizations / IAM trusted-access permissions; it degrades gracefully when denied.
- Excel can turn 12-digit account IDs into scientific notation when editing the owner CSVs. Matching also uses account names, so this
  is harmless, but format the ID column as text.

## Roadmap

Daily-grain as-now data (burn chart), opportunity detectors from the 12-month trends (steady always-on spend as commitment candidates, non-prod that is not shrinking, GCP credits ending), a printable review pack per owner, tags and `NetAmortizedCost` feeding a Capex / Opex view once Finance confirms the basis, idle-resource signals (needs usage data such as AWS Compute Optimizer or GCP Recommender), authentication for shared hosting, and a scheduled monthly report.
