# sbom-list

Aggregates Snyk SBOM + vulnerability data, reconciles it against Jira, computes SLA compliance, and
writes the daily compliance Sheet + Slack alerts. This repo **writes** the Google Sheet that
`cs-security-compliance-dashboard` reads.

## Stack & run
- Python 3 (pandas, gspread, slack-sdk, matplotlib, requests) + Google Apps Script (`*.gs`).
- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Run a script: `python <script>.py` (credentials from `.env`).

## Data sources
- **Snyk API** — CycloneDX SBOM + issue counts per product mapping.
- **Jira** `https://contentstack.atlassian.net` — SCA/SAST + VAPT issues; severity `customfield_10115`.
- **Google Sheet** `16pqLCcnWoLPvAUq6sNJCsXyc75mWHsBRcKcHH-d_oOc` — daily compliance metrics + charts.
- **Slack** channel `C07G6R3FUDU`. Service account: `json-key.json`.

## Key files
- `sbom_report.py` — Snyk component/vuln aggregation per product mapping.
- `jira_compliance_daily.py` — daily % fixed-within-SLA per Jira project.
- `jira_sla_breach_label.py` — applies `sla-breached` label to tickets resolved past SLA.
- `compliance_workbook_charts.py` — compliance charts into the Sheet.
- `jira_breach_latency_averages.py`, `sla_breach_reconcile.py` — latency stats / reconciliation.
- `mappings.json`, `jira_metrics_queries.json` — product mappings + JQL templates.
- Apps Scripts (deployed to Google, not run locally): `SBOM.gs` (9 AM refresh),
  `JiraComplianceDaily.gs` (SNYK/SCA cols A–K), `JiraComplianceDailyVapt.gs` (VAPT cols M–W; combined
  Y–AH), `JiraSlaBreachLabel.gs`.

## Conventions & gotchas
- SLA: Sev-0 14d, Sev-1 30d, Sev-2 90d, Sev-3 180d.
- Sheet columns **Y–AH** per project tab = combined SNYK+VAPT daily metrics; A–K = SNYK, M–W = VAPT.
- The `.gs` files run in Google Apps Script; edits here must be re-pasted/deployed there to take effect.
- Reconcile counts against Jira before trusting the Sheet (`data-accuracy-check`).
