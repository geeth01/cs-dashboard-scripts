"""
Multi-channel sanity aggregator: Slack → Google Sheets.

Reads sanity report messages from two Slack channels, extracts the environment
name from each message, and writes results to two tabs in the shared spreadsheet:
  - "Other-env-sanities"          : Pass / Fail / Total per suite per env
  - "Other-env-sanities-failures" : Fail% per suite per env (env name as column header)

Supported message formats:
  Pipe-delimited:
    dev18 | Suite Name :white_check_mark: | 0 Failed Modules | 118/118 Tests | 28mins 50sec

  Legacy multi-line:
    stagBlizzard, Suite Name
    *Result*: :white_check_mark: Passed

Date range: 2026-02-01 to today (weekdays only, Mon–Fri).

Usage:
  python other-env-sanities-aggregator.py
  python other-env-sanities-aggregator.py 2026-02-01
  python other-env-sanities-aggregator.py 2026-02-01 2026-06-05
"""

from __future__ import annotations

import os
import re
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"

_DEFAULT_SPREADSHEET_ID = "1rMUn-tPQpQwmj4qbdrOTWipu_sjRU0BCap11tPPFjrM"
SPREADSHEET_ID = _DEFAULT_SPREADSHEET_ID
CREDENTIALS_PATH = str(SCRIPT_DIR / "jiraproject-key.json")
OUTPUT_SHEET_NAME = "Other-env-sanities"
FAILURES_SHEET_NAME = "Other-env-sanities-failures"
DEFAULT_START_DATE = "2026-02-01"

CHANNEL_IDS = ["C06A31VD1UK", "C0836LCEZDY", "C07B5SXD29M", "C0B0T7U5X8A", "C0B623V6Y68"]

slack_token = os.environ.get("SLACK_BOT_TOKEN", "")

PASS_MARK = "✅"
FAIL_MARK = "❌"

# Only include these sanities/suites
ALLOWED_SUITES = {
    "rte full sanity - ui",
    "full sanity - ui",
    "assets full sanity - ui",
    "search full sanity - ui",
    "taxonomy full sanity - ui",
    "variants full sanity - ui",
    "extensions full sanity - ui",
    "releases20 full sanity - ui",
    "autodraft full sanity - ui",
    "cma full sanity - api",
    "cma api autodraft - api",
    "taxonomy - api",
    "cma nested global fields - api",
    "bulkdelete - api",
    "release20 - api",
    "cma basic sanity - api",
    "search full sanity - api",
    "search variants sanity - api",
    "variants - api",
    "cda full sanity - api",
}

# ---------------------------------------------------------------------------
# Copied verbatim from reference script
# ---------------------------------------------------------------------------

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
    if _env_bool("SLACK_INSECURE_SSL", False):
        warnings.warn(
            "SLACK_INSECURE_SSL is enabled: TLS verification is off.",
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


def extract_message_text(message: dict) -> str:
    """Plain text from text field, blocks, and attachments."""
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
            for field in block.get("fields") or []:
                if isinstance(field, dict) and field.get("text"):
                    chunks.append(str(field["text"]))
        elif btype == "context":
            for el in block.get("elements") or []:
                if el.get("type") in ("mrkdwn", "plain_text") and el.get("text"):
                    chunks.append(str(el["text"]))
    for att in message.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        for key in ("pretext", "text", "fallback"):
            v = att.get(key)
            if v:
                chunks.append(str(v))
        for f in att.get("fields") or []:
            if not isinstance(f, dict):
                continue
            for fk in ("title", "value", "text"):
                v = f.get(fk)
                if v:
                    chunks.append(str(v))
    return "\n".join(c for c in chunks if c)


_STATUS_REPLACEMENTS = {
    r"\*Result\*:": "",
    r"dev11,": "",
    r"dev11 ,": "",
    r"Dev11,": "",
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
    if not normalized:
        return "unknown"
    if PASS_MARK in normalized or re.search(r"\bpass(ed)?\b", normalized, re.I):
        return "pass"
    if FAIL_MARK in normalized or re.search(r"\bfail", normalized, re.I):
        return "fail"
    return "unknown"


def outcome_from_status(raw: str) -> str:
    """Return 'pass', 'fail', or 'unknown'."""
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


def _gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    return gspread.authorize(creds)


_T = TypeVar("_T")


def _run_with_sheets_retry(label: str, fn: Callable[[], _T]) -> _T:
    """Retry on Google Sheets quota errors (HTTP 429) with exponential backoff."""
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
                f"{label}: Sheets rate limit — sleeping {wait}s "
                f"(retry {attempt + 1}/{max_attempts})...",
                flush=True,
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")


def weekday_report_dates(start_d: date, end_d: date) -> list[date]:
    """Mon–Fri dates inclusive in range."""
    out: list[date] = []
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# New: config
# ---------------------------------------------------------------------------

client: WebClient = WebClient(token=slack_token, ssl=build_slack_ssl_context())


def apply_config_from_env() -> None:
    global SPREADSHEET_ID, CREDENTIALS_PATH, slack_token, OUTPUT_SHEET_NAME, FAILURES_SHEET_NAME, client
    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", _DEFAULT_SPREADSHEET_ID)
    CREDENTIALS_PATH = os.environ.get("CREDENTIALS_PATH", str(SCRIPT_DIR / "jiraproject-key.json"))
    slack_token = os.environ.get("SLACK_BOT_TOKEN", slack_token)
    OUTPUT_SHEET_NAME = os.environ.get("OUTPUT_SHEET_NAME", "Other-env-sanities")
    FAILURES_SHEET_NAME = os.environ.get("FAILURES_SHEET_NAME", "Other-env-sanities-failures")
    client = WebClient(token=slack_token, ssl=build_slack_ssl_context())


def resolve_date_range() -> tuple[datetime, datetime, date, date]:
    """Returns (slack_oldest, slack_latest, start_date, end_date)."""
    start_s = os.environ.get("START_DATE", "").strip()
    end_s = os.environ.get("END_DATE", "").strip()

    if len(sys.argv) >= 2:
        start_s = sys.argv[1].strip()
    if len(sys.argv) >= 3:
        end_s = sys.argv[2].strip()

    if not start_s:
        start_s = DEFAULT_START_DATE
    if not end_s:
        end_s = date.today().isoformat()

    start_d = datetime.strptime(start_s, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_s, "%Y-%m-%d").date()
    if end_d < start_d:
        print("END_DATE must be on or after START_DATE.", file=sys.stderr)
        sys.exit(1)

    range_start = datetime.combine(start_d, datetime.min.time()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    range_end = (
        datetime.combine(end_d, datetime.min.time()).replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        + timedelta(days=1)
    )
    return range_start, range_end, start_d, end_d


# ---------------------------------------------------------------------------
# New: parsing
# ---------------------------------------------------------------------------

_SKIP_PATTERNS = re.compile(r"<@|report|Azure-eu", re.I)

_EMOJI_PASS = re.compile(r":white_check_mark:|:heavy_check_mark:|:large_green_circle:", re.I)
_EMOJI_FAIL = re.compile(r":x:|:heavy_multiplication_x:", re.I)


def _strip_suite_emojis(s: str) -> str:
    """Remove outcome emojis and markdown asterisks from a suite segment."""
    s = _EMOJI_PASS.sub("", s)
    s = _EMOJI_FAIL.sub("", s)
    s = re.sub(r"\*+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_suite(raw: str) -> str:
    """Collapse spaces, strip markdown artifacts."""
    s = (raw or "").replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = re.sub(r"\*+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_env_and_suite(text: str) -> tuple[str, str, str] | None:
    """
    Parse a sanity result message and return (env_name, suite_name, raw_status_line).
    Returns None if the message is not a recognisable sanity result.

    Skip-pattern check is applied only to env_name (the first segment before | or ,),
    NOT to the entire line — trailing segments like "HTML Report | View Report" must
    not cause legitimate results to be discarded.
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None

    first = lines[0]

    # --- Pipe-delimited format: "env | Suite Name :emoji: | ..." ---
    if re.match(r"^[^\s,|][^,\n]*\|", first):
        parts = [p.strip() for p in first.split("|")]
        if len(parts) < 2:
            return None
        env_name = parts[0].strip()
        # Only skip-check env_name — trailing link/mention segments are irrelevant
        if _SKIP_PATTERNS.search(env_name):
            return None
        suite_segment = parts[1]
        raw_status_line = suite_segment  # emoji lives here
        suite_name = _normalize_suite(_strip_suite_emojis(suite_segment))
        if not env_name or not suite_name or len(suite_name) < 2:
            return None
        return env_name, suite_name, raw_status_line

    # --- Legacy multi-line format: "env, Suite Name\n*Result*: :emoji: ..." ---
    if re.match(r"^[^\s|][^|\n]+,", first):
        comma_idx = first.index(",")
        env_name = first[:comma_idx].strip()
        # Only skip-check env_name, not the suite portion
        if _SKIP_PATTERNS.search(env_name):
            return None
        suite_name = _normalize_suite(first[comma_idx + 1:])
        if not env_name or not suite_name or len(suite_name) < 2:
            return None
        # Find the result line among subsequent lines
        raw_status_line = ""
        for ln in lines[1:]:
            ll = ln.lower()
            if (
                "result" in ll
                or ":x:" in ll
                or ":white_check_mark:" in ll
                or "failure" in ll
                or "fail" in ll
                or "passed" in ll
                or "success" in ll
                or ll.startswith("failed")
            ):
                raw_status_line = ln
                break
        if not raw_status_line and len(lines) > 1:
            raw_status_line = lines[1]
        return env_name, suite_name, raw_status_line

    return None


# ---------------------------------------------------------------------------
# New: filtering & normalization
# ---------------------------------------------------------------------------

def should_include_suite(suite_name: str) -> bool:
    """Filter to only whitelisted suites. Check normalized (lowercase) name against ALLOWED_SUITES."""
    normalized = suite_name.lower().strip()
    return normalized in ALLOWED_SUITES


def should_skip_env(env_name: str) -> bool:
    """
    Filter out unwanted environments (case-insensitive).
    Return True to skip, False to keep.
    """
    # Strip leading asterisks and whitespace
    cleaned = env_name.strip().lstrip("*").strip()
    env_lower = cleaned.lower()
    # Skip if starts with 'dev'
    if env_lower.startswith("dev"):
        return True
    # Skip if contains 'bliz'
    if "bliz" in env_lower:
        return True
    return False


def normalize_env_name(env_name: str) -> str:
    """
    Normalize environment names for deduplication.
    1. Strip leading asterisks and whitespace
    2. Lowercase everything
    3. Strip regional prefix 'na' (naStag → stag, naProd → prod)
    """
    # Strip leading asterisks and whitespace
    cleaned = env_name.strip().lstrip("*").strip()
    normalized = cleaned.lower()
    if normalized.startswith("na"):
        # Remove 'na' prefix, but only if it's followed by a capital letter (regex check)
        # E.g., naStag → stag, naProd → prod, but 'nat' stays 'nat'
        rest = normalized[2:]
        # Check if the original before lowercasing had a capital after 'na'
        if rest and rest[0].isalpha():
            return rest
    return normalized


# ---------------------------------------------------------------------------
# New: Slack fetching
# ---------------------------------------------------------------------------

def fetch_channel_messages(
    channel_id: str,
    range_start: datetime,
    range_end: datetime,
) -> list[tuple[str, str, str, datetime]]:
    """
    Fetch sanity result events from one channel.
    Returns list of (env_name, suite_name, outcome, message_datetime).
    """
    results: list[tuple[str, str, str, datetime]] = []
    cursor = None
    start_ts = str(range_start.timestamp())
    end_ts = str(range_end.timestamp())

    try:
        while True:
            response = client.conversations_history(
                channel=channel_id,
                limit=100,
                cursor=cursor,
                oldest=start_ts,
                latest=end_ts,
                timeout=120,
            )
            messages = response["messages"]
            cursor = response.get("response_metadata", {}).get("next_cursor")

            for message in messages:
                dt = datetime.fromtimestamp(float(message["ts"]))

                # Weekends skip
                if dt.weekday() >= 5:
                    continue
                if not (range_start <= dt <= range_end):
                    continue

                text = extract_message_text(message)
                tl = text.lower()

                # Quick pre-filter: must mention a result indicator
                looks_like_result = (
                    ":x:" in tl
                    or ":white_check_mark:" in tl
                    or "result" in tl
                    or "passed" in tl
                    or "failure" in tl
                    or "success" in tl
                )
                if not looks_like_result:
                    continue

                parsed = extract_env_and_suite(text)
                if parsed is None:
                    continue

                env_name, suite_name, raw_status_line = parsed
                outcome = outcome_from_status(raw_status_line)
                results.append((env_name, suite_name, outcome, dt))

            if not cursor:
                break

    except SlackApiError as e:
        print(f"Slack error on channel {channel_id}: {e.response.get('error')}", flush=True)

    return results


# ---------------------------------------------------------------------------
# New: aggregation
# ---------------------------------------------------------------------------

def aggregate_results(
    events: list[tuple[str, str, str, datetime]],
) -> dict[str, dict[str, dict[str, int]]]:
    """
    Aggregate events into {suite → {env_normalized → {pass, fail, total}}}.
    Applies filtering (suite whitelist, env blacklist) and normalization (lowercase, na prefix strip).
    'unknown' outcomes count toward total only.
    """
    agg: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"pass": 0, "fail": 0, "total": 0})
    )
    for env_name, suite_name, outcome, _dt in events:
        # Filter to whitelisted suites
        if not should_include_suite(suite_name):
            continue
        # Skip blacklisted environments
        if should_skip_env(env_name):
            continue
        # Normalize env name (lowercase, strip 'na' prefix)
        env_normalized = normalize_env_name(env_name)
        # Aggregate at normalized key
        cell = agg[suite_name][env_normalized]
        cell["total"] += 1
        if outcome == "pass":
            cell["pass"] += 1
        elif outcome == "fail":
            cell["fail"] += 1
    return agg


def build_sorted_env_list(aggregation: dict) -> list[str]:
    envs: set[str] = set()
    for suite_data in aggregation.values():
        envs.update(suite_data.keys())
    return sorted(envs, key=str.lower)


def build_sorted_suite_list(aggregation: dict) -> list[str]:
    return sorted(aggregation.keys(), key=str.lower)


# ---------------------------------------------------------------------------
# New: sheet writing
# ---------------------------------------------------------------------------

def get_or_create_worksheet(spreadsheet, sheet_name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_name, rows=300, cols=100)


def build_header_row(env_list: list[str]) -> list[str]:
    """Header for Other-env-sanities: Suite Name | env Pass | env Fail | env Total | ..."""
    header = ["Suite Name"]
    for env in env_list:
        header += [f"{env} Pass", f"{env} Fail", f"{env} Total"]
    return header


def build_sheet_rows(
    suite_list: list[str],
    env_list: list[str],
    aggregation: dict,
) -> list[list]:
    rows = []
    for suite in suite_list:
        row: list = [suite]
        for env in env_list:
            cell = aggregation.get(suite, {}).get(env, {})
            t = cell.get("total", 0)
            if t == 0:
                row += ["NA", "NA", "NA"]
            else:
                row += [cell.get("pass", 0), cell.get("fail", 0), t]
        rows.append(row)
    return rows


def build_failures_header_row(env_list: list[str]) -> list[str]:
    """Header for Other-env-sanities-failures: Suite Name | env | env | ..."""
    return ["Suite Name"] + list(env_list)


def build_failures_sheet_rows(
    suite_list: list[str],
    env_list: list[str],
    aggregation: dict,
) -> list[list]:
    rows = []
    for suite in suite_list:
        row: list = [suite]
        for env in env_list:
            cell = aggregation.get(suite, {}).get(env, {})
            f = cell.get("fail", 0)
            t = cell.get("total", 0)
            row.append(f"{f / t * 100:.1f}%" if t > 0 else "NA")
        rows.append(row)
    return rows


def write_sheet(
    worksheet: gspread.Worksheet,
    header: list[str],
    rows: list[list],
) -> None:
    _run_with_sheets_retry("clear sheet", worksheet.clear)
    all_data = [header] + rows
    _run_with_sheets_retry(
        "write sheet",
        lambda: worksheet.update("A1", all_data, value_input_option="RAW"),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_env_file()
    apply_config_from_env()

    if not slack_token.strip():
        print("Set SLACK_BOT_TOKEN in .env (or environment).", file=sys.stderr)
        sys.exit(1)

    range_start, range_end, start_d, end_d = resolve_date_range()
    print(f"Date range: {start_d} → {end_d} (weekdays only)", flush=True)

    if not os.path.isfile(CREDENTIALS_PATH):
        print(f"Missing credentials file: {CREDENTIALS_PATH}", file=sys.stderr)
        sys.exit(1)

    gc = _gspread_client()
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    ws_main = _run_with_sheets_retry(
        "open/create main worksheet",
        lambda: get_or_create_worksheet(spreadsheet, OUTPUT_SHEET_NAME),
    )
    ws_failures = _run_with_sheets_retry(
        "open/create failures worksheet",
        lambda: get_or_create_worksheet(spreadsheet, FAILURES_SHEET_NAME),
    )

    all_events: list[tuple[str, str, str, datetime]] = []
    for ch_id in CHANNEL_IDS:
        print(f"Fetching channel {ch_id}...", flush=True)
        events = fetch_channel_messages(ch_id, range_start, range_end)
        print(f"  → {len(events)} sanity results found", flush=True)
        all_events.extend(events)

    if not all_events:
        print("No sanity results found in the given date range. Sheets not updated.")
        return

    aggregation = aggregate_results(all_events)
    env_list = build_sorted_env_list(aggregation)
    suite_list = build_sorted_suite_list(aggregation)

    print(
        f"\nAggregated: {len(suite_list)} suites × {len(env_list)} environments "
        f"({', '.join(env_list)})",
        flush=True,
    )

    # Write main summary sheet: Suite | env Pass | env Fail | env Total | ...
    write_sheet(ws_main, build_header_row(env_list), build_sheet_rows(suite_list, env_list, aggregation))
    print(f"Written to tab '{OUTPUT_SHEET_NAME}'.", flush=True)

    # Write failures sheet: Suite | env (Fail%) | env (Fail%) | ...
    write_sheet(ws_failures, build_failures_header_row(env_list), build_failures_sheet_rows(suite_list, env_list, aggregation))
    print(f"Written to tab '{FAILURES_SHEET_NAME}'.", flush=True)

    print(f"\nDone.", flush=True)


if __name__ == "__main__":
    main()
