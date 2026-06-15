#!/usr/bin/env python3
"""
Sanity flakiness tracker: S3 → Google Sheets.

For each enabled sanity × environment in config/sanities.yaml, scans S3 run folders
within START_DATE..END_DATE in configurable day batches, checks for FailedModules.txt,
and writes aggregated pass/fail and module frequency data to 4 tabs in the output
Google Sheet. The Run Log tab includes a clickable link to each S3 HTML report.

Usage:
    python sanity_s3_report.py

Key env vars (set in .env):
    START_DATE          YYYY-MM-DD (inclusive)
    END_DATE            YYYY-MM-DD (inclusive)
    BATCH_DAYS          Days per scan batch (default: 30)
    SPREADSHEET_ID      Google Sheet ID for output
    CREDENTIALS_PATH    Path to service account JSON
    AWS_ACCESS_KEY_ID   Optional — leave blank for anonymous S3 access (VPN)
    AWS_SECRET_ACCESS_KEY
    AWS_DEFAULT_REGION  Defaults to us-east-1
    S3_BUCKET           Defaults to sanity-reports-and-screenshots
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import boto3
import gspread
import yaml
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

BUCKET = os.getenv("S3_BUCKET", "sanity-reports-and-screenshots")
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
CREDENTIALS_PATH = os.getenv("CREDENTIALS_PATH", str(_SCRIPT_DIR / "service-account.json"))
SHEETS_DELAY = float(os.getenv("SHEETS_API_DELAY_SECONDS", "2"))
BATCH_DAYS = int(os.getenv("BATCH_DAYS", "30"))

_raw_start = os.getenv("START_DATE", "")
_raw_end = os.getenv("END_DATE", "")
START_DATE: Optional[date] = datetime.strptime(_raw_start, "%Y-%m-%d").date() if _raw_start else None
END_DATE: Optional[date] = datetime.strptime(_raw_end, "%Y-%m-%d").date() if _raw_end else None

FAILED_MODULES_FILE = "FailedModules.txt"
S3_REPORT_BASE_URL = f"https://{BUCKET}.s3.amazonaws.com/index.html"

# Sheet tab names
TAB_SUMMARY = "Summary"
TAB_BY_ENV = "By Environment"
TAB_MODULE_FAILURES = "Module Failures"
TAB_RUN_LOG = "Run Log"

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    sanity: str
    env: str
    s3_prefix: str      # e.g. gcpProdVenusUIAssets — used to build report URL
    run_date: str       # YYYYMMDD
    run_time: str       # HHMMSS
    passed: bool
    failed_modules: list[str] = field(default_factory=list)

    def report_url(self) -> str:
        return f"{S3_REPORT_BASE_URL}#{self.s3_prefix}/report/{self.run_date}/{self.run_time}/"


@dataclass
class SanityEnvStats:
    sanity: str
    env: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    module_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def pass_pct(self) -> str:
        return f"{100 * self.passed / self.total:.1f}%" if self.total else "N/A"


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3_client():
    key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    if key and secret:
        return boto3.client(
            "s3", region_name=REGION,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
        )
    return boto3.client("s3", region_name=REGION, config=Config(signature_version=UNSIGNED))


def _list_subfolders(s3, bucket: str, prefix: str) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    folders: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes") or []:
            name = cp["Prefix"][len(prefix):].rstrip("/")
            if name:
                folders.append(name)
    return sorted(folders)


def _object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def _read_object_text(s3, bucket: str, key: str) -> str:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8", errors="replace")


def _date_in_batch(date_str: str, batch_start: date, batch_end: date) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y%m%d").date()
        return batch_start <= d <= batch_end
    except ValueError:
        return False


# ── Batching ──────────────────────────────────────────────────────────────────

def _date_batches(start: date, end: date, batch_days: int):
    """Yield (batch_start, batch_end, batch_num, total_batches) tuples."""
    batches = []
    current = start
    while current <= end:
        batch_end = min(current + timedelta(days=batch_days - 1), end)
        batches.append((current, batch_end))
        current = batch_end + timedelta(days=1)
    total = len(batches)
    for i, (bs, be) in enumerate(batches, 1):
        yield bs, be, i, total


# ── Core scan ─────────────────────────────────────────────────────────────────

def scan_sanity_env(
    s3, bucket: str, sanity_name: str, env: str, s3_prefix: str,
    batch_start: date, batch_end: date,
) -> list[RunRecord]:
    """Scan runs for one sanity × env within a single date batch."""
    records: list[RunRecord] = []
    report_prefix = f"{s3_prefix}/report/"

    date_folders = _list_subfolders(s3, bucket, report_prefix)
    date_folders_in_batch = [d for d in date_folders if _date_in_batch(d, batch_start, batch_end)]

    for date_folder in date_folders_in_batch:
        time_prefix = f"{report_prefix}{date_folder}/"
        time_folders = _list_subfolders(s3, bucket, time_prefix)

        for time_folder in time_folders:
            failed_key = f"{report_prefix}{date_folder}/{time_folder}/{FAILED_MODULES_FILE}"
            failed_exists = _object_exists(s3, bucket, failed_key)

            failed_modules: list[str] = []
            if failed_exists:
                try:
                    content = _read_object_text(s3, bucket, failed_key)
                    failed_modules = [
                        line.strip() for line in content.splitlines() if line.strip()
                    ]
                except ClientError:
                    failed_modules = ["(error reading FailedModules.txt)"]

            records.append(RunRecord(
                sanity=sanity_name,
                env=env,
                s3_prefix=s3_prefix,
                run_date=date_folder,
                run_time=time_folder,
                passed=not failed_exists,
                failed_modules=failed_modules,
            ))

    return records


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate(records: list[RunRecord]) -> dict[tuple[str, str], SanityEnvStats]:
    stats: dict[tuple[str, str], SanityEnvStats] = {}
    for r in records:
        key = (r.sanity, r.env)
        if key not in stats:
            stats[key] = SanityEnvStats(sanity=r.sanity, env=r.env)
        s = stats[key]
        s.total += 1
        if r.passed:
            s.passed += 1
        else:
            s.failed += 1
            for m in r.failed_modules:
                s.module_counts[m] += 1
    return stats


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _gspread_client() -> gspread.Client:
    if not os.path.isfile(CREDENTIALS_PATH):
        print(f"ERROR: credentials file not found: {CREDENTIALS_PATH}", file=sys.stderr)
        sys.exit(1)
    creds = Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def _get_or_create_tab(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=100, cols=20)


def _write_tab(sh: gspread.Spreadsheet, title: str, rows: list[list]) -> None:
    ws = _get_or_create_tab(sh, title)
    ws.clear()
    time.sleep(SHEETS_DELAY)

    if not rows:
        print(f"  Wrote 0 rows → '{title}' (no data)")
        return

    # Resize to fit before writing so the sheet never rejects rows
    ws.resize(rows=max(len(rows) + 10, 100), cols=len(rows[0]))
    time.sleep(1)

    # Use named args — gspread 6.x changed positional order and silently
    # misbehaves when range_name is passed first without a keyword
    ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
    time.sleep(SHEETS_DELAY)

    ws.freeze(rows=1)
    ws.set_basic_filter()
    time.sleep(SHEETS_DELAY)
    print(f"  Wrote {len(rows)} rows → '{title}'")


def _run_date_display(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def _clear_all_tabs(sh: gspread.Spreadsheet) -> None:
    for title in [TAB_SUMMARY, TAB_BY_ENV, TAB_MODULE_FAILURES, TAB_RUN_LOG]:
        try:
            ws = sh.worksheet(title)
            ws.clear()
            print(f"  Cleared '{title}'")
            time.sleep(1)
        except gspread.WorksheetNotFound:
            pass


def write_sheets(
    records: list[RunRecord],
    stats: dict[tuple[str, str], SanityEnvStats],
    period: str,
) -> None:
    if not SPREADSHEET_ID:
        print("ERROR: SPREADSHEET_ID not set in .env", file=sys.stderr)
        sys.exit(1)

    gc = _gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    print(f"Writing to sheet: {sh.title}")
    print("Clearing all tabs first...")
    _clear_all_tabs(sh)
    print()

    # ── Tab 1: Summary ────────────────────────────────────────────────────
    sanity_totals: dict[str, SanityEnvStats] = {}
    for (sanity, env), s in stats.items():
        if sanity not in sanity_totals:
            sanity_totals[sanity] = SanityEnvStats(sanity=sanity, env="ALL")
        agg = sanity_totals[sanity]
        agg.total += s.total
        agg.passed += s.passed
        agg.failed += s.failed
        for mod, cnt in s.module_counts.items():
            agg.module_counts[mod] += cnt

    summary_rows = [["Sanity", "Total Runs", "Pass", "Fail", "Pass %", "Period"]]
    for sanity, agg in sorted(sanity_totals.items()):
        summary_rows.append([sanity, agg.total, agg.passed, agg.failed, agg.pass_pct, period])
    _write_tab(sh, TAB_SUMMARY, summary_rows)

    # ── Tab 2: By Environment ─────────────────────────────────────────────
    by_env_rows = [["Sanity", "Environment", "Total Runs", "Pass", "Fail", "Pass %"]]
    for (sanity, env), s in sorted(stats.items()):
        by_env_rows.append([sanity, env, s.total, s.passed, s.failed, s.pass_pct])
    _write_tab(sh, TAB_BY_ENV, by_env_rows)

    # ── Tab 3: Module Failures ────────────────────────────────────────────
    # One row per sanity × environment × module — filter any column independently.
    module_rows = [["Sanity", "Environment", "Module", "Fail Count"]]
    for (sanity, env), s in sorted(stats.items()):
        for module, count in sorted(s.module_counts.items(), key=lambda x: -x[1]):
            module_rows.append([sanity, env, module, count])
    _write_tab(sh, TAB_MODULE_FAILURES, module_rows)

    # ── Tab 4: Run Log ────────────────────────────────────────────────────
    # One row per run × module. PASS runs get one row with Module = "".
    # "Report" column is a clickable HYPERLINK to the S3 HTML report.
    run_log_rows = [["Date", "Time", "Sanity", "Environment", "Status", "Module", "Report"]]
    for r in sorted(records, key=lambda x: (x.run_date, x.run_time, x.sanity, x.env), reverse=True):
        date_str = _run_date_display(r.run_date)
        link = f'=HYPERLINK("{r.report_url()}", "View Report")'
        if r.passed or not r.failed_modules:
            run_log_rows.append([date_str, r.run_time, r.sanity, r.env, "PASS", "", link])
        else:
            for module in r.failed_modules:
                run_log_rows.append([date_str, r.run_time, r.sanity, r.env, "FAIL", module, link])
    _write_tab(sh, TAB_RUN_LOG, run_log_rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def load_config() -> tuple[str, list[dict]]:
    cfg_path = _SCRIPT_DIR / "config" / "sanities.yaml"
    if not cfg_path.exists():
        print(f"ERROR: config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    return cfg.get("bucket", BUCKET), cfg.get("sanities", [])


def main() -> None:
    if not START_DATE or not END_DATE:
        print("ERROR: Set START_DATE and END_DATE in .env (format: YYYY-MM-DD)", file=sys.stderr)
        sys.exit(1)

    period = f"{START_DATE} to {END_DATE}"
    total_days = (END_DATE - START_DATE).days + 1
    num_batches = -(-total_days // BATCH_DAYS)  # ceiling division
    print(f"Sanity S3 report | {period}")
    print(f"Bucket : {BUCKET}")
    print(f"Batches: {num_batches} × {BATCH_DAYS}-day windows\n")

    bucket, sanities = load_config()
    enabled = [(s["name"], s.get("environments", {})) for s in sanities if s.get("enabled")]

    try:
        s3 = _s3_client()
    except NoCredentialsError:
        print("ERROR: No AWS credentials. Set AWS_ACCESS_KEY_ID/SECRET in .env", file=sys.stderr)
        sys.exit(1)

    all_records: list[RunRecord] = []

    for batch_start, batch_end, batch_num, total_batches in _date_batches(START_DATE, END_DATE, BATCH_DAYS):
        print(f"── Batch {batch_num}/{total_batches}: {batch_start} → {batch_end} ──")
        batch_count = 0

        for sanity_name, envs in enabled:
            for env, prefix in envs.items():
                if not prefix:
                    continue

                try:
                    records = scan_sanity_env(s3, bucket, sanity_name, env, prefix, batch_start, batch_end)
                except ClientError as exc:
                    code = exc.response["Error"]["Code"]
                    print(f"  WARNING: {sanity_name}/{env} — S3 error ({code}), skipping.")
                    continue

                if records:
                    passes = sum(1 for r in records if r.passed)
                    fails = len(records) - passes
                    print(f"  {sanity_name} / {env}: {len(records)} runs ({passes} pass, {fails} fail)")
                    all_records.extend(records)
                    batch_count += len(records)

        print(f"  Batch total: {batch_count} runs\n")

    if not all_records:
        print("No run data found. Check VPN connection, date range, and S3 prefixes in sanities.yaml.")
        return

    stats = aggregate(all_records)
    print(f"Total runs collected: {len(all_records)}")
    print("Writing to Google Sheets...\n")
    write_sheets(all_records, stats, period)
    print("\nDone.")


if __name__ == "__main__":
    main()
