"""
Dev11 CMS sanity (FY27 thread classification): Slack → Google Sheets.

Supports two Slack message formats:

  Legacy multi-line:
    dev11, Suite Name
    *Result*: :white_check_mark: Passed

  Pipe-delimited:
    Dev11 | Suite Name :white_check_mark: | 0 Failed Modules | 118/118 Tests | 28mins 50sec
    Dev11 | Suite Name :x: | 1 Failed Modules | 5/6 Tests | 12mins 16sec

Per weekday D and suite S:
  - FY27Status: last run — pass emoji; fail + Bug-Infra/Product in thread → pass emoji;
                fail + flaky or no reason → fail emoji
  - FY27Flaky / FY27Bug-Product / FY27Bug-Infra: counts of failed runs in each bucket
  - FY27TotalRuns: all runs that day

On Friday date runs (Saturday cron), also updates FY27TC with test counts.

Suite list is fetched dynamically from the Teams sheet (column D) at runtime.
Missing suites are auto-appended to writing sheet tabs.

Configuration: .env next to this script (START_DATE, END_DATE, ...).

Usage:
  python dev11-build-send-cms-table-jenkins-fy27-thread.py
  python dev11-build-send-cms-table-jenkins-fy27-thread.py YYYY-MM-DD
  python3 dev11-build-send-cms-table-jenkins-fy27-thread.py YYYY-MM-DD YYYY-MM-DD
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
from typing import Literal, TypeVar

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

PASS_MARK = "✅"
FAIL_MARK = "❌"

FailureBucket = Literal["flaky", "bug_infra", "bug_product"]

# --- Paths / constants ---
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"

_DEFAULT_SPREADSHEET_ID = "1rMUn-tPQpQwmj4qbdrOTWipu_sjRU0BCap11tPPFjrM"
Teams_SHEET_ID = "1Bs3KpKMHyYcn5eSie-r8QCrUalI7a5235o9Lzl5ku2U"

# Filled by apply_config_from_env() after .env is loaded
SPREADSHEET_ID = _DEFAULT_SPREADSHEET_ID
CREDENTIALS_PATH = str(SCRIPT_DIR / "jiraproject-key.json")
SHEET_FY27_STATUS = "FY27SanityStatus"
SHEET_FY27_FLAKY = "FY27Flaky"
SHEET_FY27_BUG_PRODUCT = "FY27Bug-Product"
SHEET_FY27_BUG_INFRA = "FY27Bug-Infra"
SHEET_FY27_TOTAL_RUNS_NEW = "FY27TotalRuns"
SHEET_FY27_TC = "FY27TC"

slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
channel_id = "C07SUNJ3ZEV"

# (suite line, result line, message time local, message ts, thread_ts, test_count)
slack_events: list[tuple[str, str, datetime, str, str, str]] = []


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
    global SPREADSHEET_ID, CREDENTIALS_PATH
    global SHEET_FY27_STATUS, SHEET_FY27_FLAKY, SHEET_FY27_BUG_PRODUCT
    global SHEET_FY27_BUG_INFRA, SHEET_FY27_TOTAL_RUNS_NEW, SHEET_FY27_TC
    global slack_token, channel_id, client

    SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", _DEFAULT_SPREADSHEET_ID)
    CREDENTIALS_PATH = os.environ.get("CREDENTIALS_PATH", str(SCRIPT_DIR / "jiraproject-key.json"))
    SHEET_FY27_STATUS = os.environ.get("SHEET_FY27_STATUS", "FY27SanityStatus")
    SHEET_FY27_FLAKY = os.environ.get("SHEET_FY27_FLAKY", "FY27Flaky")
    SHEET_FY27_BUG_PRODUCT = os.environ.get("SHEET_FY27_BUG_PRODUCT", "FY27Bug-Product")
    SHEET_FY27_BUG_INFRA = os.environ.get("SHEET_FY27_BUG_INFRA", "FY27Bug-Infra")
    SHEET_FY27_TOTAL_RUNS_NEW = os.environ.get("SHEET_FY27_TOTAL_RUNS_NEW", "FY27TotalRuns")
    SHEET_FY27_TC = os.environ.get("SHEET_FY27_TC", "FY27TC")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", slack_token)
    channel_id = os.environ.get("SLACK_CHANNEL_ID", channel_id)
    client = WebClient(token=slack_token, ssl=build_slack_ssl_context())


client = WebClient(token=slack_token, ssl=build_slack_ssl_context())


def resolve_date_range() -> tuple[datetime, datetime, date, date]:
    """
    Returns (slack_oldest, slack_latest, start_date, end_date).
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
            "  python dev11-build-send-cms-table-jenkins-fy27-thread.py START [END]",
            file=sys.stderr,
        )
        sys.exit(1)

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


def weekday_report_dates(start_d: date, end_d: date) -> list[date]:
    """Mon–Fri dates inclusive in range (no weekend columns or counts)."""
    out: list[date] = []
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


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
    """Return 'pass', 'fail', or 'unknown'."""
    if not normalized:
        return "unknown"
    if PASS_MARK in normalized or re.search(r"\bpass(ed)?\b", normalized, re.I):
        return "pass"
    if FAIL_MARK in normalized or re.search(r"\bfail", normalized, re.I):
        return "fail"
    return "unknown"


def normalize_suite_name(raw: str) -> str:
    """
    Align Slack suite lines with Google Sheet column A (strip dev11 prefix, NBSPs, spaces).
    Only Rest / GraphQL preview titles get bold asterisks removed.
    """
    s = (raw or "").replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = s.strip()
    s = re.sub(r"^\*+", "", s)
    s = re.sub(r"\*+$", "", s)
    s = re.sub(r"^dev11\s*,\s*", "", s, flags=re.I)
    s = re.sub(r"^dev11\s+", "", s, flags=re.I)
    s = s.strip()
    sl = s.lower()
    if "graphql preview service" in sl:
        s = s.replace("*", "")
    elif "rest preview service" in sl:
        s = s.replace("*", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def build_output_order(suite_order: list[str], seen_normalized_suites: set[str]) -> list[str]:
    """Stable list: fetched sheet order first, then any extra suites from Slack (A–Z)."""
    canonical = set(suite_order)
    extras = sorted(s for s in seen_normalized_suites if s and s not in canonical)
    return list(suite_order) + extras


def outcome_from_status(raw: str) -> str:
    """Pass/fail/unknown using normalized text and raw Slack markers (e.g. :x:)."""
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
    """Plain text, blocks, and attachment text (failure reasons often live only there)."""
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


def _normalize_for_failure_match(text: str) -> str:
    """Normalize unicode spaces/dashes/asterisks so failure-reason regexes match reliably."""
    if not text:
        return ""
    s = str(text)
    for u in (
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        "﻿",
    ):
        s = s.replace(u, " ")
    for old in ("–", "—", "−", "﹘", "‐", "‑"):
        s = s.replace(old, "-")
    s = re.sub(r"\*+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_CHECK_PREFIX = (
    r"(?:\:white_check_mark\:|\:heavy_check_mark\:|[✅✔]️?)\s*"
)

_FAILURE_REASON_PATTERNS: list[tuple[FailureBucket, re.Pattern[str]]] = [
    (
        "bug_infra",
        re.compile(
            _CHECK_PREFIX + r"Failure\s+reason:\s*Bug\s*-\s*Infra\b",
            re.I,
        ),
    ),
    (
        "bug_product",
        re.compile(
            _CHECK_PREFIX + r"Failure\s+reason:\s*Bug\s*-\s*Product\b",
            re.I,
        ),
    ),
    (
        "flaky",
        re.compile(_CHECK_PREFIX + r"Failure\s+reason:\s*Flaky\b", re.I),
    ),
]

_FAILURE_REASON_FALLBACK: list[tuple[FailureBucket, re.Pattern[str]]] = [
    ("bug_infra", re.compile(r"Failure\s+reason:\s*Bug\s*-\s*Infra\b", re.I)),
    ("bug_product", re.compile(r"Failure\s+reason:\s*Bug\s*-\s*Product\b", re.I)),
    ("flaky", re.compile(r"Failure\s+reason:\s*Flaky\b", re.I)),
]


def match_failure_reason_category(text: str) -> FailureBucket | None:
    """If text contains a failure-reason line, return its category (first match order)."""
    if not text or not str(text).strip():
        return None
    norm = _normalize_for_failure_match(text)
    if not norm:
        return None
    for bucket, pat in _FAILURE_REASON_PATTERNS:
        if pat.search(norm):
            return bucket
    for bucket, pat in _FAILURE_REASON_FALLBACK:
        if pat.search(norm):
            return bucket
    return None


def classify_failure_from_thread(reply_messages: list[dict]) -> FailureBucket:
    """Among thread messages with a failure reason, use the latest by ts. Default: flaky."""
    best_ts: float | None = None
    best: FailureBucket = "flaky"
    for msg in reply_messages:
        body = extract_message_text(msg)
        cat = match_failure_reason_category(body)
        if cat is None:
            continue
        ts = float(msg.get("ts", 0))
        if best_ts is None or ts > best_ts:
            best_ts = ts
            best = cat
    return best


def _slack_replies_delay_seconds() -> float:
    raw = (os.environ.get("SLACK_REPLIES_DELAY_SECONDS") or "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def fetch_conversation_replies(thread_ts: str) -> list[dict]:
    """All messages in the thread (including parent), paginated."""
    out: list[dict] = []
    cursor = None
    delay = _slack_replies_delay_seconds()
    while True:
        response = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=200,
            cursor=cursor,
            timeout=120,
        )
        out.extend(response["messages"])
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        if delay > 0:
            time.sleep(delay)
    return out


def _extract_test_count_from_lines(lines: list[str]) -> str:
    """Extract total test count from legacy multi-line format lines."""
    for line in lines:
        if "*Total Tests*" in line:
            m = re.search(r"\*Total Tests\*:\s*(\d+)", line)
            if m:
                return m.group(1)
    for line in lines:
        m = re.search(r"\((\d+)\s*/\s*(\d+)\s*Passed", line)
        if m:
            return m.group(2)
        m = re.search(r"\((\d+)\s*/\s*(\d+)\s*tests passed", line, re.I)
        if m:
            return m.group(2)
    return "NA"


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

                msg_ts = str(message["ts"])
                thread_ts = str(message.get("thread_ts") or message["ts"])

                text = extract_message_text(message)
                tl = text.lower()
                first_line, second_line, test_count = "", "", "NA"

                looks_like_result = "dev11" in tl and (
                    "result" in tl
                    or ":x:" in tl
                    or ":white_check_mark:" in tl
                    or "failure" in tl
                    or "passed" in tl
                    or "success" in tl
                )

                if looks_like_result:
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                    first_text_line = lines[0] if lines else ""

                    # Pipe format: "Dev11 | Suite Name :emoji: | N Failed Modules | N/N Tests | ..."
                    if re.match(r"^dev11\s*\|", first_text_line, re.I):
                        pipe_parts = [p.strip() for p in first_text_line.split("|")]
                        if len(pipe_parts) >= 2:
                            suite_segment = pipe_parts[1]
                            # Determine outcome from emoji before stripping
                            if re.search(r":white_check_mark:", suite_segment, re.I):
                                second_line = ":white_check_mark:"
                            elif re.search(r":x:", suite_segment, re.I):
                                second_line = ":x:"
                            # Strip emojis and asterisks to get clean suite name
                            suite_segment = re.sub(r"\s*:white_check_mark:\s*", "", suite_segment, flags=re.I)
                            suite_segment = re.sub(r"\s*:x:\s*", "", suite_segment, flags=re.I)
                            first_line = suite_segment.replace("*", "").strip()
                        # Extract test count: denominator from "N/N Tests"
                        m = re.search(r"(\d+)/(\d+)\s*Tests", first_text_line, re.I)
                        if m:
                            test_count = m.group(2)
                    else:
                        # Legacy multi-line format
                        lp_legacy_shift = (
                            "Live Preview" in text
                            and len(lines) >= 2
                            and "dev11" not in lines[0].lower()
                        )
                        if lp_legacy_shift:
                            first_line = lines[1]
                            second_line = lines[2] if len(lines) > 2 else ""
                            test_count = _extract_test_count_from_lines(lines[3:])
                        else:
                            dev_idx = next(
                                (
                                    i
                                    for i, ln in enumerate(lines)
                                    if "dev11" in ln.lower()
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
                                test_count = _extract_test_count_from_lines(lines[dev_idx + 1:])

                if not first_line and "blocks" in message:
                    fl, sl = "", ""
                    block_lines_for_count: list[str] = []
                    for block in message["blocks"]:
                        if block["type"] in ("section", "header"):
                            block_text = block.get("text", {}).get("text", "").strip()
                            lines_b = block_text.split("\n")
                            if not any(ln.strip() for ln in lines_b):
                                continue
                            if (
                                "Live Preview" in block_text
                                and len(lines_b) > 1
                                and "dev11" not in (lines_b[0] or "").lower()
                            ):
                                fl = lines_b[1].strip() if len(lines_b) > 1 else ""
                                sl = lines_b[2].strip() if len(lines_b) > 2 else ""
                                block_lines_for_count = lines_b[3:]
                                break
                            if len(lines_b) > 0 and "dev11" in lines_b[0].lower():
                                fl = lines_b[0].strip()
                                sl = lines_b[1].strip() if len(lines_b) > 1 else ""
                                block_lines_for_count = lines_b[2:]
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
                    if block_lines_for_count:
                        test_count = _extract_test_count_from_lines(block_lines_for_count)

                if first_line and not re.search(
                    r"<@|report|Azure-eu", first_line, re.IGNORECASE
                ):
                    slack_events.append(
                        (first_line, second_line, message_date_obj, msg_ts, thread_ts, test_count)
                    )

            if not cursor:
                break

    except SlackApiError as e:
        print(f"Error fetching messages: {e.response['error']}")
    except KeyError as e:
        print(f"Unexpected message format: {e}")


def last_fy27_status_symbol(
    runs: list[tuple[str, FailureBucket | None]],
) -> str:
    """Status for the chronologically last run only."""
    if not runs:
        return ""
    outcome, fb = runs[-1]
    if outcome == "pass":
        return PASS_MARK
    if outcome == "fail":
        if fb in ("bug_infra", "bug_product"):
            return PASS_MARK
        return FAIL_MARK
    return ""


def build_daily_metrics_thread(
    events: list[tuple[str, str, datetime, str, str, str]],
    report_dates: list[date],
    range_start_d: date,
    range_end_d: date,
    order: list[str],
) -> dict[date, dict[str, dict[str, int | str]]]:
    """
    Per weekday D and suite: FY27Status (last), flaky/bug counts (failed runs only),
    total runs, test count (last run). Resolves failure bucket via conversations.replies.
    """
    bucket: dict[tuple[date, str], list[tuple[str, str, datetime, str, str]]] = defaultdict(list)

    for suite, raw_status, dt, _msg_ts, thread_ts, test_count in events:
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
        bucket[(d, suite_k)].append((outcome, raw_status, dt, thread_ts, test_count))

    for key in bucket:
        bucket[key].sort(key=lambda x: x[2])

    delay = _slack_replies_delay_seconds()
    metrics: dict[date, dict[str, dict[str, int | str]]] = {}

    for d in report_dates:
        metrics[d] = {}
        for suite in order:
            entries = bucket.get((d, suite), [])
            runs_for_status: list[tuple[str, FailureBucket | None]] = []
            flaky_c = bug_p = bug_i = 0
            last_test_count = "NA"

            for outcome, raw_status, _dt, thread_ts, test_count in entries:
                fb: FailureBucket | None = None
                if outcome == "fail":
                    try:
                        replies = fetch_conversation_replies(thread_ts)
                        fb = classify_failure_from_thread(replies)
                    except SlackApiError as e:
                        print(f"Error fetching thread {thread_ts}: {e.response.get('error')}")
                        fb = "flaky"
                    if delay > 0:
                        time.sleep(delay)
                    if fb == "flaky":
                        flaky_c += 1
                    elif fb == "bug_product":
                        bug_p += 1
                    elif fb == "bug_infra":
                        bug_i += 1
                runs_for_status.append((outcome, fb))
                last_test_count = test_count  # keep last run's count

            metrics[d][suite] = {
                "status": last_fy27_status_symbol(runs_for_status),
                "flaky": flaky_c,
                "bug_product": bug_p,
                "bug_infra": bug_i,
                "total": len(entries),
                "test_count": last_test_count,
            }

    return metrics


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


def fetch_suite_order_from_sheet(gc) -> list[str]:
    """Fetch ordered suite names from the Teams sheet, column D (skips header row)."""
    ws = gc.open_by_key(Teams_SHEET_ID).worksheet("Teams")
    col_d = ws.col_values(4)  # Column D = Sanity names
    return [s.strip() for s in col_d[1:] if s.strip()]


def ensure_suites_in_sheets(worksheets: list[gspread.Worksheet], suite_order: list[str]) -> None:
    """
    For each worksheet, insert any suites from suite_order that are missing from Column A,
    placing each new suite at the same relative position as in suite_order so it mirrors
    the Teams sheet order (e.g. if a suite is at index 47 in Teams, it lands at row 48
    in the writing sheet — right after its nearest predecessor, before the summary row).
    """
    for ws in worksheets:
        all_vals = ws.get_all_values()

        # Build: normalized_name → 1-based row number (row 1 = header)
        name_to_row: dict[str, int] = {}
        for row_idx, row in enumerate(all_vals):
            if not row:
                continue
            nk = normalize_suite_name(row[0].strip())
            if nk:
                name_to_row[nk] = row_idx + 1  # 1-based

        existing = set(name_to_row.keys())
        missing = [s for s in suite_order if normalize_suite_name(s) not in existing]
        if not missing:
            continue

        inserted = 0
        for suite in missing:
            suite_idx = suite_order.index(suite)
            nk = normalize_suite_name(suite)

            # Scan backwards in suite_order to find the nearest predecessor already in the sheet
            insert_after_row = 1  # fallback: insert right after header (becomes row 2)
            for j in range(suite_idx - 1, -1, -1):
                pred_nk = normalize_suite_name(suite_order[j])
                if pred_nk in name_to_row:
                    insert_after_row = name_to_row[pred_nk]
                    break

            insert_at = insert_after_row + 1

            _run_with_sheets_retry(
                f"{ws.title} insert '{suite}'",
                lambda row=insert_at, s=suite: ws.insert_rows([[s]], row=row),
            )
            print(f"{ws.title}: inserted '{suite}' at row {insert_at}.")
            inserted += 1

            # Shift all tracked row numbers >= insert_at down by 1 to stay accurate
            for k in list(name_to_row.keys()):
                if name_to_row[k] >= insert_at:
                    name_to_row[k] += 1
            name_to_row[nk] = insert_at

        if inserted:
            print(f"{ws.title}: {inserted} suite(s) inserted to match Teams sheet order.")


def _parse_mmdd(s: str) -> tuple[int, int] | None:
    """Parse a 'MM/DD' header string → (month, day), or None if not a date header."""
    try:
        parts = str(s).strip().split("/")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except (ValueError, AttributeError):
        pass
    return None


def _ensure_date_column(
    worksheet: gspread.Worksheet, header: list[str], date_header: str
) -> int:
    """
    Ensure date_header exists as a column in the worksheet. If it is absent, insert it
    at the chronologically correct position (MM/DD order) rather than always appending,
    so runs executed out of order don't leave date columns scrambled.

    Modifies *header* in-place to reflect the new column.
    Returns the 1-based column index of date_header.
    """
    if date_header in header:
        return header.index(date_header) + 1

    new = _parse_mmdd(date_header)
    insert_col = len(header) + 1  # default: append at end

    if new:
        new_mmdd = new[0] * 100 + new[1]
        date_positions: list[tuple[int, int]] = []
        for i, h in enumerate(header):
            parsed = _parse_mmdd(h)
            if parsed:
                date_positions.append((i, parsed[0] * 100 + parsed[1]))

        if date_positions:
            max_existing = max(p[1] for p in date_positions)
            # Year-boundary guard: if new date is Jan–Jun and all existing are Jul–Dec
            # (or vice-versa with a gap > 6 months), new date is next calendar year → append
            cross_year = max_existing - new_mmdd > 600
            if not cross_year:
                for col_0idx, existing_mmdd in date_positions:
                    if new_mmdd < existing_mmdd:
                        insert_col = col_0idx + 1  # convert 0-based to 1-based
                        break

    if insert_col <= len(header):
        # Insert a blank column at the right position using the Sheets batchUpdate API
        sheet_id = worksheet._properties["sheetId"]
        worksheet.spreadsheet.batch_update({
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": insert_col - 1,   # 0-based, inclusive
                        "endIndex": insert_col,          # 0-based, exclusive
                    },
                    "inheritFromBefore": False,
                }
            }]
        })
        header.insert(insert_col - 1, date_header)
    else:
        header.append(date_header)

    worksheet.update_cell(1, insert_col, date_header)
    return insert_col


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
        col_index = _ensure_date_column(worksheet, header, date_header)
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
    """Write integer counts per suite for one date column."""

    def _once() -> None:
        sheet_data = worksheet.get_all_values()
        if not sheet_data:
            print(f"{worksheet_name}: empty sheet; skipped {date_header}.")
            return
        header = list(sheet_data[0])
        existing_rows = sheet_data[1:]

        col_index = _ensure_date_column(worksheet, header, date_header)

        row_map = {row[0].strip(): idx + 2 for idx, row in enumerate(existing_rows) if row}
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


def upload_test_count_sheet(
    worksheet: gspread.Worksheet,
    worksheet_name: str,
    date_header: str,
    suite_to_count: dict[str, str],
) -> None:
    """Write test counts per suite for one date column. 'NA' values become 0."""

    def _once() -> None:
        sheet_data = worksheet.get_all_values()
        if not sheet_data:
            print(f"{worksheet_name}: empty sheet; skipped {date_header}.")
            return
        header = list(sheet_data[0])
        existing_rows = sheet_data[1:]

        col_index = _ensure_date_column(worksheet, header, date_header)

        row_map = {row[0].strip(): idx + 2 for idx, row in enumerate(existing_rows) if row}
        updates = []
        for test_name, row_idx in row_map.items():
            nk = normalize_suite_name(test_name)
            raw_val = suite_to_count.get(nk, suite_to_count.get(test_name, "NA"))
            if str(raw_val).strip().upper() == "NA":
                val: int | str = 0
            else:
                try:
                    val = int(raw_val)
                except (ValueError, TypeError):
                    val = 0
            updates.append(
                {"range": gspread.utils.rowcol_to_a1(row_idx, col_index), "values": [[val]]}
            )
        if updates:
            worksheet.batch_update(updates)
        print(f"{worksheet_name}: updated column {date_header} (test_count).")

    _run_with_sheets_retry(f"{worksheet_name} {date_header} (test_count)", _once)


def main() -> None:
    load_env_file()
    apply_config_from_env()

    if not slack_token.strip() or not channel_id.strip():
        print(
            "Set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in .env (or environment).",
            file=sys.stderr,
        )
        sys.exit(1)

    range_start, range_end, start_d, end_d = resolve_date_range()
    report_dates = weekday_report_dates(start_d, end_d)
    if not report_dates:
        print("No weekdays in the given date range; nothing to report.")
        sys.exit(0)

    if not os.path.isfile(CREDENTIALS_PATH):
        print(f"Missing {CREDENTIALS_PATH}; cannot proceed.")
        sys.exit(1)

    gc = _gspread_client()

    # Fetch dynamic suite order from Teams sheet
    suite_order = _run_with_sheets_retry(
        "fetch suite order", lambda: fetch_suite_order_from_sheet(gc)
    )
    print(f"Fetched {len(suite_order)} suites from Teams sheet.")

    slack_events.clear()
    fetch_messages(range_start, range_end)

    seen = {
        normalize_suite_name(s)
        for s, _, _, _, _, _ in slack_events
        if normalize_suite_name(s)
    }
    order = build_output_order(suite_order, seen)

    empty_row: dict[str, int | str] = {
        "status": "",
        "flaky": 0,
        "bug_product": 0,
        "bug_infra": 0,
        "total": 0,
        "test_count": "NA",
    }
    if slack_events:
        metrics = build_daily_metrics_thread(
            slack_events, report_dates, start_d, end_d, order
        )
    else:
        metrics = {d: {s: dict(empty_row) for s in order} for d in report_dates}
        print("No Slack result rows in this window; all zeros / blank status.")

    def _open_tabs():
        sh = gc.open_by_key(SPREADSHEET_ID)
        return (
            sh,
            sh.worksheet(SHEET_FY27_STATUS),
            sh.worksheet(SHEET_FY27_FLAKY),
            sh.worksheet(SHEET_FY27_BUG_PRODUCT),
            sh.worksheet(SHEET_FY27_BUG_INFRA),
            sh.worksheet(SHEET_FY27_TOTAL_RUNS_NEW),
        )

    sh, ws_status, ws_flaky, ws_bug_p, ws_bug_i, ws_totals = _run_with_sheets_retry(
        "open spreadsheet + tabs", _open_tabs
    )

    # Ensure all suites exist as rows in every writing sheet before uploading
    # Use suite_order (Teams sheet only), not order, to avoid adding stray Slack messages
    ensure_suites_in_sheets([ws_status, ws_flaky, ws_bug_p, ws_bug_i, ws_totals], suite_order)

    pause = _sheets_delay_between_dates_seconds()
    for idx, d in enumerate(report_dates):
        dh = d.strftime("%m/%d")
        suite_status = {s: str(metrics[d][s]["status"]) for s in order}
        suite_flaky = {s: int(metrics[d][s]["flaky"]) for s in order}
        suite_bug_p = {s: int(metrics[d][s]["bug_product"]) for s in order}
        suite_bug_i = {s: int(metrics[d][s]["bug_infra"]) for s in order}
        suite_totals = {s: int(metrics[d][s]["total"]) for s in order}
        upload_status_sheet(ws_status, SHEET_FY27_STATUS, dh, suite_status)
        update_counts_sheet(ws_flaky, SHEET_FY27_FLAKY, dh, suite_flaky, "flaky")
        update_counts_sheet(ws_bug_p, SHEET_FY27_BUG_PRODUCT, dh, suite_bug_p, "bug_product")
        update_counts_sheet(ws_bug_i, SHEET_FY27_BUG_INFRA, dh, suite_bug_i, "bug_infra")
        update_counts_sheet(ws_totals, SHEET_FY27_TOTAL_RUNS_NEW, dh, suite_totals, "total")
        if pause > 0 and idx + 1 < len(report_dates):
            time.sleep(pause)

    # Update test counts (FY27TC): normally only on Friday's data (Saturday run),
    # but FORCE_TEST_COUNT=1 in env overrides the day check for ad-hoc backfills.
    if start_d.weekday() == 4 or _env_bool("FORCE_TEST_COUNT", False):
        ws_tc = sh.worksheet(SHEET_FY27_TC)
        ensure_suites_in_sheets([ws_tc], suite_order)
        for idx, d in enumerate(report_dates):
            dh = d.strftime("%m/%d")
            suite_tc = {s: str(metrics[d][s].get("test_count", "NA")) for s in order}
            upload_test_count_sheet(ws_tc, SHEET_FY27_TC, dh, suite_tc)
            if pause > 0 and idx + 1 < len(report_dates):
                time.sleep(pause)


if __name__ == "__main__":
    main()
