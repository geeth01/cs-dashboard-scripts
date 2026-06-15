# cms-dev11-reports

Jenkins build & test reporting for the CMS dev11 (and legacy dev9) environments. Fetches build/test
results, aggregates pass/fail/flaky/sanity metrics, and publishes daily/weekly Plotly tables to Google
Sheets + Slack threads.

## Stack & run
- Python 3 (pandas, plotly, gspread, slack-sdk, openpyxl). Service account: `jiraproject-key.json`.
- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Run: `python <script>.py` (config from `.env`; see `.env.example`).

## Data sources
- **Jenkins API** — build status + test results (dev11, dev9).
- **Slack** — primary channel `C07SUNJ3ZEV` plus cross-env channels; reads via `conversations.replies`,
  posts threaded reports.
- **Google Sheets** tabs: `FY27Status`, `FY27Flaky`, `FY27Bug-Product`, `FY27Bug-Infra`,
  `FY27TotalRunsNew`, `Other-env-sanities`, `Other-env-sanities-failures`.

## Key files
- `dev11-build-send-cms-table-jenkins-unified.py` — main unified build metrics + Slack table.
- `dev11-build-send-cms-table-jenkins-fy27-thread.py` — FY27 threaded reporting.
- `dev11-sanity-test-count.py` — sanity execution counts.
- `other-env-sanities-aggregator.py` — multi-channel cross-env sanity status.
- `dev9-build-send-cms-table-jenkins-unified.py` — legacy dev9.

## Conventions & gotchas
- Plotly renders tables to PNG (`table_image.png`) before Slack upload — brand the styling
  (`contentstack-brand-guidelines`) for leadership-facing tables.
- Flaky vs Bug-Product vs Bug-Infra classification drives the Sheet tabs — keep consistent with
  `cs-dev11-governance` failure classification.
