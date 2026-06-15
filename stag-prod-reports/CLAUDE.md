# stag-prod-reports

Sanity flakiness tracker: scans S3 run folders across 10 environments, detects failures via
`FailedModules.txt`, and writes aggregated pass/fail + module frequency data to Google Sheets.

## Stack
Python 3 (boto3, gspread, PyYAML, python-dotenv). Service account: `service-account.json`.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SPREADSHEET_ID and CREDENTIALS_PATH
```

## Files
- `discover_prefixes.py` — run once on VPN to list all S3 prefixes; use output to fill `sanities.yaml`
- `sanity_s3_report.py` — main script: scans S3 → writes 4 Google Sheet tabs
- `config/sanities.yaml` — THE config: sanity → {environment: s3_prefix} mapping

## Workflow
1. Connect to VPN.
2. `python discover_prefixes.py` — see all available S3 prefixes.
3. Edit `config/sanities.yaml`: fill in the prefix values, set `enabled: true`.
4. `python sanity_s3_report.py` — writes results to the output sheet.

## Adding / removing a sanity
Edit `config/sanities.yaml` only — no code changes needed:
- Add: copy a sanity block, fill in name + prefixes, set `enabled: true`
- Remove: set `enabled: false` (keeps history) or delete the block
- Skip an environment: leave its prefix as `""`

## Output sheet tabs
| Tab | Content |
|-----|---------|
| Summary | One row per sanity — combined pass%, top failing modules |
| By Environment | One row per sanity × env |
| Module Failures | Ranked: which modules fail most often per sanity |
| Run Log | Full audit trail — one row per run |

## S3 structure
```
{s3_prefix}/report/{YYYYMMDD}/{HHMMSS}/
    FailedModules.txt   ← present = run failed; absent = run passed
```
Bucket: `sanity-reports-and-screenshots` (VPN-only access).

## Env vars (see .env.example)
`START_DATE`, `END_DATE`, `SPREADSHEET_ID`, `CREDENTIALS_PATH`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`
