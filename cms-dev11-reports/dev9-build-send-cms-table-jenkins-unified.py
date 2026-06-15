"""
Dev9 CMS sanity: Slack → CSV → Google Sheets.

Weekends: result messages that fall on Saturday or Sunday are ignored. Only **weekdays**
in [START_DATE, END_DATE] get sheet columns and CSV columns.

Per weekday **D** and suite **S**:
  - **FY27SanityStatus**: last run that day → pass/fail emoji (or blank if no run)
  - **FY27Failures**: number of failed runs that day
  - **FY27TotalRuns**: number of runs that day (pass + fail + unknown)

Configuration: ``.env`` next to this script (START_DATE, END_DATE, CSV_ONLY, …).

Usage:
  python dev9-build-send-cms-table-jenkins-unified.py
  python dev9-build-send-cms-table-jenkins-unified.py YYYY-MM-DD
  python dev9-build-send-cms-table-jenkins-unified.py YYYY-MM-DD YYYY-MM-DD
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import ssl
import sys
import time
import warnings
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

PASS_MARK = "\u2705"
FAIL_MARK = "\u274c"

# --- Paths / constants ---
SCRIPT_DIR = Path(__file__).resolve().parent
CSV_FILE = str(SCRIPT_DIR / "sanity_messages.csv")
CSV_COPY = str(SCRIPT_DIR / "destination.csv")
ENV_FILE = SCRIPT_DIR / ".env"

_DEFAULT_SPREADSHEET_ID = "1VFA8MO_GlM67dYD7g7Y0_yKhcWu0vN07fUgD1jRx7a4"
_DEFAULT_SLACK = os.environ.get("SLACK_BOT_TOKEN", "")
_DEFAULT_CHANNEL = "C05RN8UCS9K"

# Filled by apply_config_from_env() after .env is loaded
SPREADSHEET_ID = _DEFAULT_SPREADSHEET_ID
CREDENTIALS_PATH = str(SCRIPT_DIR / "jiraproject-key.json")
SHEET_STATUS = "FY27SanityStatus"
SHEET_FAILURES = "FY27Failures"
SHEET_TOTAL_RUNS = "FY27TotalRuns"
slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
channel_id = "C05RN8UCS9K"
# Raw Slack result rows: (suite first line, result second line, message time local)
slack_events: list[tuple[str, str, datetime]] = []


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs into os.environ (does not overwrite existing vars)."""
    p = path or ENV_FILE
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def build_slack_ssl_context() -> ssl.SSLContext:
    """
    macOS / Python.org builds often lack a usable CA store for urllib; using certifi
    fixes [SSL: CERTIFICATE_VERIFY_FAILED]. Corporate proxies can set SSL_CERT_FILE or
    SLACK_INSECURE_SSL=1 (last resort — disables verification).
    """
    if _env_bool("SLACK_INSECURE_SSL", False):
        warnings.warn(
            "SLACK_INSECURE_SSL is enabled: TLS verification is off. Use only if required.",
            UserWarning,
            stacklevel=2,
        )
        return ssl._create_unverified_context()

    cert_file = (os.environ.get("SSL_CERT_FILE") or "").strip()
    if cert_file and os.path.isfile(cert_file):
        return ssl.create_default_context(cafile=cert_file)

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

    return ssl.create_default_context()


def apply_config_from_env() -> None:
    """Apply os.environ to module-level Slack/sheet settings (call after load_env_file)."""
    global SPREADSHEET_ID, CREDENTIALS_PATH, SHEET_STATUS, SHEET_FAILURES, SHEET_TOTAL_RUNS
    global slack_token, channel_id, client

    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", _DEFAULT_SPREADSHEET_ID)
    CREDENTIALS_PATH = os.environ.get("CREDENTIALS_PATH", str(SCRIPT_DIR / "jiraproject-key.json"))
    SHEET_STATUS = os.environ.get("SHEET_STATUS", "FY27SanityStatus")
    SHEET_FAILURES = os.environ.get("SHEET_FAILURES", "FY27Failures")
    SHEET_TOTAL_RUNS = os.environ.get("SHEET_TOTAL_RUNS", "FY27TotalRuns")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", _DEFAULT_SLACK)
    channel_id = os.environ.get("SLACK_CHANNEL_ID", _DEFAULT_CHANNEL)
    client = WebClient(token=slack_token, ssl=build_slack_ssl_context())


client = WebClient(token=slack_token, ssl=build_slack_ssl_context())


def resolve_date_range() -> tuple[datetime, datetime, date, date]:
    """
    Returns (slack_oldest_5am, slack_latest_5am_next, start_date, end_date).

    Slack API: ``oldest`` = range_start timestamp, ``latest`` = end of window (inclusive
    of messages up to 05:00 on the day after ``end_date``).
    """
    start_s = os.environ.get("START_DATE", "").strip()
    end_s = os.environ.get("END_DATE", "").strip()

    if len(sys.argv) >= 2:
        start_s = sys.argv[1].strip()
    if len(sys.argv) >= 3:
        end_s = sys.argv[2].strip()
    elif not end_s and start_s:
        end_s = start_s

    if not start_s or not end_s:
        print(
            "Set START_DATE and END_DATE in .env (YYYY-MM-DD), or pass:\n"
            "  python dev9-build-send-cms-table-jenkins-unified.py START [END]",
            file=sys.stderr,
        )
        sys.exit(1)

    start_d = datetime.strptime(start_s, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_s, "%Y-%m-%d").date()
    if end_d < start_d:
        print("END_DATE must be on or after START_DATE.", file=sys.stderr)
        sys.exit(1)

    range_start = datetime.combine(start_d, datetime.min.time()).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    range_end = datetime.combine(end_d, datetime.min.time()).replace(
        hour=5, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    return range_start, range_end, start_d, end_d


def weekday_report_dates(start_d: date, end_d: date) -> list[date]:
    """Mon–Fri dates inclusive in range (no Saturday/Sunday columns or counts)."""
    out: list[date] = []
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


_STATUS_REPLACEMENTS = {
    r"\*Result\*:": "",
    r"dev9,": "",
    r"dev9 ,": "",
    r"Dev9,": "",
    r"Passed": "",
    r"Success": "",
    r"Failure": "",
    r"\bResult\b": "",
    r"\bModules\b": "",
    r"\(": "",
    r"\)": "",
    r":": "",
    r"tests passed": "",
    r'"': "",
    r"mins": "m",
    r"sec.": "s",
    r"\*": "",
}


def normalize_status_text(raw: str) -> str:
    if not raw or not str(raw).strip():
        return ""
    s = str(raw)
    s = re.sub(r":white_check_mark:", PASS_MARK, s, flags=re.I)
    s = re.sub(r":x:", FAIL_MARK, s, flags=re.I)
    s = re.sub(r":X:", FAIL_MARK, s)
    for pat, repl in _STATUS_REPLACEMENTS.items():
        s = re.sub(pat, repl, s, flags=re.I)
    return s.strip()


def classify_outcome(normalized: str) -> str:
    """Return 'pass', 'fail', or 'unknown'."""
    if not normalized:
        return "unknown"
    if PASS_MARK in normalized or re.search(r"\bpass(ed)?\b", normalized, re.I):
        return "pass"
    if FAIL_MARK in normalized or re.search(r"\bfail", normalized, re.I):
        return "fail"
    return "unknown"


def last_status_symbol(outcomes: list[str]) -> str:
    """Last run of the day as sheet emoji (blank if no decisive run)."""
    for o in reversed(outcomes):
        if o == "pass":
            return PASS_MARK
        if o == "fail":
            return FAIL_MARK
    return ""


def normalize_suite_name(raw: str) -> str:
    """
    Align Slack suite lines with Google Sheet column A (strip ``dev9,`` prefix, NBSPs,
    spaces). Only **Rest** / **GraphQL** preview titles get Slack ``*bold*`` asterisks
    removed so they match the sheet; other suites are unchanged (avoids breaking names).
    """
    s = (raw or "").replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
    s = s.strip()
    s = re.sub(r"^\*+", "", s)
    s = re.sub(r"\*+$", "", s)
    s = re.sub(r"^dev9\s*,\s*", "", s, flags=re.I)
    s = re.sub(r"^dev9\s+", "", s, flags=re.I)
    s = s.strip()
    sl = s.lower()
    if "graphql preview service" in sl:
        s = s.replace("*", "")
    elif "rest preview service" in sl:
        s = s.replace("*", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def build_output_order(seen_normalized_suites: set[str]) -> list[str]:
    """Stable list: canonical ``all_order`` first, then any other suites from Slack (A–Z)."""
    canonical = set(all_order)
    extras = sorted(s for s in seen_normalized_suites if s and s not in canonical)
    return list(all_order) + extras


def outcome_from_status(raw: str) -> str:
    """Pass/fail/unknown using normalized text and raw Slack markers (e.g. ``:x:``)."""
    normalized = normalize_status_text(raw)
    c = classify_outcome(normalized)
    if c != "unknown":
        return c
    raw_l = (raw or "").lower()
    if ":x:" in raw_l or ":heavy_multiplication_x:" in raw_l or "failure" in raw_l:
        return "fail"
    if (
        ":white_check_mark:" in raw_l
        or ":heavy_check_mark:" in raw_l
        or ":large_green_circle:" in raw_l
    ):
        return "pass"
    return "unknown"


def extract_message_text(message: dict) -> str:
    """Plain ``text`` plus visible text from ``section`` / ``header`` / ``rich_text`` blocks."""
    chunks: list[str] = []
    t = message.get("text")
    if t:
        chunks.append(str(t))
    for block in message.get("blocks") or []:
        btype = block.get("type")
        if btype == "rich_text":
            for element in block.get("elements") or []:
                if element.get("type") == "rich_text_section":
                    for sub in element.get("elements") or []:
                        if sub.get("type") == "text":
                            chunks.append(sub.get("text", ""))
        elif btype in ("section", "header"):
            bt = block.get("text") or {}
            if isinstance(bt, dict) and bt.get("text"):
                chunks.append(str(bt["text"]))
    return "\n".join(c for c in chunks if c)


# --- Default CSV row list (suites from Slack not listed here are appended alphabetically) ---
all_order = [
  "Full Sanity - UI",
  "Assets Full Sanity - UI",
  "Releases20 Full Sanity - UI",
  "CMA Full Sanity - API",
  "BulkDelete - API",
  "Release20 - API",
  "AssetManagement20 - API",
  "Asset Managment Test - UI",
  "AssetPicker Full Sanity - UI"
] 


def _cleanup_local_files() -> None:
    for path in (CSV_FILE, CSV_COPY):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"Error deleting {path}: {e}")


def fetch_messages(start_date: datetime, end_date: datetime | None = None) -> None:
    try:
        cursor = None
        start_timestamp = str(start_date.timestamp())
        end_timestamp = str(end_date.timestamp()) if end_date else None

        while True:
            response = client.conversations_history(
                channel=channel_id,
                limit=100,
                cursor=cursor,
                oldest=start_timestamp,
                latest=end_timestamp,
                timeout=120,
            )
            messages = response["messages"]
            cursor = response.get("response_metadata", {}).get("next_cursor")

            for message in messages:
                message_date_obj = datetime.fromtimestamp(float(message["ts"]))
                if end_date:
                    if not (start_date <= message_date_obj <= end_date):
                        continue
                else:
                    if message_date_obj < start_date:
                        continue

                text = extract_message_text(message)
                tl = text.lower()
                first_line, second_line = "", ""

                looks_like_result = "dev9" in tl and (
                    "result" in tl
                    or ":x:" in tl
                    or ":white_check_mark:" in tl
                    or "failure" in tl
                    or "passed" in tl
                    or "success" in tl
                )
                if looks_like_result:
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                    if lines:
                        # Legacy Live Preview layout: line0 was not the dev9 suite (e.g. bot
                        # prefix); suite on line1, result on line2. New layout: dev9 + Live
                        # Preview on line0, result on line1 — use normal dev9 parsing.
                        lp_legacy_shift = (
                            "Live Preview" in text
                            and len(lines) >= 2
                            and "dev9" not in lines[0].lower()
                        )
                        if lp_legacy_shift:
                            first_line = lines[1]
                            second_line = lines[2] if len(lines) > 2 else ""
                        else:
                            dev_idx = next(
                                (
                                    i
                                    for i, ln in enumerate(lines)
                                    if "dev9" in ln.lower()
                                ),
                                None,
                            )
                            if dev_idx is not None:
                                first_line = lines[dev_idx]
                                result_idx = None
                                for j in range(dev_idx + 1, len(lines)):
                                    lj = lines[j].lower()
                                    if (
                                        "result" in lj
                                        or ":x:" in lj
                                        or ":white_check_mark:" in lj
                                        or "failure" in lj
                                        or "fail" in lj
                                        or "passed" in lj
                                        or "success" in lj
                                        or lj.startswith("failed")
                                    ):
                                        result_idx = j
                                        break
                                if result_idx is not None:
                                    second_line = lines[result_idx]
                                elif dev_idx + 1 < len(lines):
                                    second_line = lines[dev_idx + 1]

                # Legacy block walk (original jenkins scripts) if flattened text missed the suite
                if not first_line and "blocks" in message:
                    fl, sl = "", ""
                    for block in message["blocks"]:
                        if block["type"] in ("section", "header"):
                            block_text = block.get("text", {}).get("text", "").strip()
                            lines_b = block_text.split("\n")
                            if not any(ln.strip() for ln in lines_b):
                                continue
                            # Live Preview: old layout had dev9 on line 1, not line 0
                            if (
                                "Live Preview" in block_text
                                and len(lines_b) > 1
                                and "dev9" not in (lines_b[0] or "").lower()
                            ):
                                fl = lines_b[1].strip() if len(lines_b) > 1 else ""
                                sl = lines_b[2].strip() if len(lines_b) > 2 else ""
                                break
                            if len(lines_b) > 0 and "dev9" in lines_b[0].lower():
                                fl = lines_b[0].strip()
                                sl = lines_b[1].strip() if len(lines_b) > 1 else ""
                                break
                            if not fl and len(lines_b) > 0:
                                fl = lines_b[0].strip()
                            elif fl and len(lines_b) > 0:
                                sl = lines_b[0].strip()
                                break
                        elif block["type"] == "section" and fl:
                            block_text = block.get("text", {}).get("text", "").strip()
                            if block_text:
                                sl = block_text
                                break
                    first_line, second_line = fl, sl

                if first_line and not re.search(
                    r"<@|report|Azure-eu", first_line, re.IGNORECASE
                ):
                    slack_events.append((first_line, second_line, message_date_obj))

            if not cursor:
                break

    except SlackApiError as e:
        print(f"Error fetching messages: {e.response['error']}")
    except KeyError as e:
        print(f"Unexpected message format: {e}")


def build_daily_metrics(
    events: list[tuple[str, str, datetime]],
    report_dates: list[date],
    range_start_d: date,
    range_end_d: date,
    order: list[str],
) -> dict[date, dict[str, dict[str, int | str]]]:
    """Per weekday D and suite: last status symbol, total runs, failure count."""
    bucket: dict[tuple[date, str], list[tuple[str, datetime]]] = defaultdict(list)

    for suite, raw_status, dt in events:
        d = dt.date()
        if d < range_start_d or d > range_end_d:
            continue
        if d.weekday() >= 5:
            continue
        if d not in report_dates:
            continue
        suite_k = normalize_suite_name(suite)
        if not suite_k:
            continue
        outcome = outcome_from_status(raw_status)
        bucket[(d, suite_k)].append((outcome, dt))

    for key in bucket:
        bucket[key].sort(key=lambda x: x[1])

    metrics: dict[date, dict[str, dict[str, int | str]]] = {}
    for d in report_dates:
        metrics[d] = {}
        for suite in order:
            entries = bucket.get((d, suite), [])
            outcomes = [o for o, _ in entries]
            metrics[d][suite] = {
                "last": last_status_symbol(outcomes),
                "total": len(entries),
                "failures": sum(1 for o in outcomes if o == "fail"),
            }
    return metrics


def write_metrics_csv(
    order: list[str],
    metrics: dict[date, dict[str, dict[str, int | str]]],
    report_dates: list[date],
) -> None:
    header = ["Sanity"]
    for d in report_dates:
        dh = d.strftime("%m/%d")
        header.extend([f"{dh} Last", f"{dh} TotalRuns", f"{dh} Failures"])
    rows = [header]
    for suite in order:
        row: list[str | int] = [suite]
        for d in report_dates:
            m = metrics[d][suite]
            row.extend([m["last"], m["total"], m["failures"]])
        rows.append(row)
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    shutil.copy(CSV_FILE, CSV_COPY)


def _gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    return gspread.authorize(creds)


_T = TypeVar("_T")


def _run_with_sheets_retry(label: str, fn: Callable[[], _T]) -> _T:
    """Retry on Google Sheets read/write quota (HTTP 429) with exponential backoff."""
    max_attempts = 8
    for attempt in range(max_attempts):
        try:
            return fn()
        except APIError as e:
            err = str(e).lower()
            quota_hit = any(
                x in err for x in ("429", "quota", "rate", "exceeded", "resource_exhausted")
            )
            if not quota_hit or attempt == max_attempts - 1:
                raise
            wait = min(2 ** (attempt + 1), 90)
            print(
                f"{label}: Sheets rate limit / quota — sleeping {wait}s "
                f"(retry {attempt + 1}/{max_attempts})...",
                flush=True,
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _sheets_delay_between_dates_seconds() -> float:
    raw = (os.environ.get("SHEETS_API_DELAY_SECONDS") or "3").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.0


def upload_status_sheet(
    worksheet: gspread.Worksheet,
    worksheet_name: str,
    date_header: str,
    suite_to_status: dict[str, str],
) -> None:
    def _once() -> None:
        sheet_data = worksheet.get_all_values()
        if not sheet_data:
            print(f"{worksheet_name}: empty sheet; skipped {date_header}.")
            return
        header = list(sheet_data[0])
        if date_header not in header:
            worksheet.update_cell(1, len(header) + 1, date_header)
            header.append(date_header)
        col_index = header.index(date_header) + 1
        updates = []
        for i in range(1, len(sheet_data)):
            row = sheet_data[i]
            row_value = row[0].strip() if row else ""
            nk = normalize_suite_name(row_value)
            if nk in suite_to_status:
                status_val = suite_to_status[nk]
            elif row_value in suite_to_status:
                status_val = suite_to_status[row_value]
            else:
                continue
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(i + 1, col_index),
                    "values": [[status_val]],
                }
            )
        if updates:
            worksheet.batch_update(updates)
        print(f"{worksheet_name}: updated column {date_header}.")

    _run_with_sheets_retry(f"{worksheet_name} {date_header}", _once)


def update_counts_sheet(
    worksheet: gspread.Worksheet,
    worksheet_name: str,
    date_header: str,
    suite_to_count: dict[str, int],
    mode: str,
) -> None:
    """Write integer counts per suite for one date column (failures or total runs)."""

    def _once() -> None:
        sheet_data = worksheet.get_all_values()
        if not sheet_data:
            print(f"{worksheet_name}: empty sheet; skipped {date_header}.")
            return
        header = list(sheet_data[0])
        existing_rows = sheet_data[1:]

        if date_header not in header:
            worksheet.update_cell(1, len(header) + 1, date_header)
            header.append(date_header)

        row_map = {row[0].strip(): idx + 2 for idx, row in enumerate(existing_rows) if row}
        col_index = header.index(date_header) + 1
        updates = []
        for test_name, row_idx in row_map.items():
            nk = normalize_suite_name(test_name)
            val = int(suite_to_count.get(nk, suite_to_count.get(test_name, 0)))
            updates.append(
                {"range": gspread.utils.rowcol_to_a1(row_idx, col_index), "values": [[val]]}
            )
        if updates:
            worksheet.batch_update(updates)
        print(f"{worksheet_name}: updated column {date_header} ({mode}).")

    _run_with_sheets_retry(f"{worksheet_name} {date_header} ({mode})", _once)


def main() -> None:
    load_env_file()
    apply_config_from_env()

    range_start, range_end, start_d, end_d = resolve_date_range()
    report_dates = weekday_report_dates(start_d, end_d)
    if not report_dates:
        print("No weekdays in the given date range; nothing to report.")
        sys.exit(0)

    csv_only = _env_bool("CSV_ONLY", False) or _env_bool("SKIP_GOOGLE_SHEETS", False)

    _cleanup_local_files()
    slack_events.clear()

    fetch_messages(range_start, range_end)
    seen = {normalize_suite_name(s) for s, _, _ in slack_events if normalize_suite_name(s)}
    order = build_output_order(seen)

    if slack_events:
        metrics = build_daily_metrics(
            slack_events, report_dates, start_d, end_d, order
        )
    else:
        metrics = {
            d: {s: {"last": "", "total": 0, "failures": 0} for s in order}
            for d in report_dates
        }
        print("No Slack result rows in this window; CSV will be all zeros / blank status.")

    write_metrics_csv(order, metrics, report_dates)
    print(f"Wrote CSV: {CSV_FILE} ({os.path.getsize(CSV_FILE)} bytes)")

    if csv_only:
        print("CSV_ONLY/SKIP_GOOGLE_SHEETS set — skipping Google Sheets.")
        return

    if not os.path.isfile(CREDENTIALS_PATH):
        print(f"Missing {CREDENTIALS_PATH}; skip Google Sheets.")
        return

    gc = _gspread_client()

    def _open_workbook_and_tabs() -> tuple[
        gspread.Spreadsheet,
        gspread.Worksheet,
        gspread.Worksheet,
        gspread.Worksheet,
    ]:
        sh = gc.open_by_key(SPREADSHEET_ID)
        return (
            sh,
            sh.worksheet(SHEET_STATUS),
            sh.worksheet(SHEET_FAILURES),
            sh.worksheet(SHEET_TOTAL_RUNS),
        )

    _, ws_status, ws_failures, ws_totals = _run_with_sheets_retry(
        "open spreadsheet + tabs", _open_workbook_and_tabs
    )

    pause = _sheets_delay_between_dates_seconds()
    for idx, d in enumerate(report_dates):
        dh = d.strftime("%m/%d")
        suite_status = {s: str(metrics[d][s]["last"]) for s in order}
        suite_fails = {s: int(metrics[d][s]["failures"]) for s in order}
        suite_totals = {s: int(metrics[d][s]["total"]) for s in order}
        upload_status_sheet(ws_status, SHEET_STATUS, dh, suite_status)
        update_counts_sheet(ws_failures, SHEET_FAILURES, dh, suite_fails, "failures")
        update_counts_sheet(ws_totals, SHEET_TOTAL_RUNS, dh, suite_totals, "total")
        if pause > 0 and idx + 1 < len(report_dates):
            time.sleep(pause)


if __name__ == "__main__":
    main()
