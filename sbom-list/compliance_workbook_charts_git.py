#!/usr/bin/env python3
"""
Leadership 2×2 charts from a Google Sheet (columns Y–AH on each project tab).

Same layout as the former Excel export: the Y–AH block matches A–K but omits Daily-Compliance (10 columns).
Authenticate with a Google Cloud service account JSON key; share the spreadsheet with that service account email.

Portfolio cumulative metrics are built from **all** daily deltas (``Σ`` per date across 19 project tabs, then ``cumsum``)
so end-of-range ``Cumulative-Fixed-Total`` / ``Cumulative-Fixed-Within_SLA`` align Summary row “Engineering” and
Σ(last row cumulative per sheet), including SNYK+VAPT (same as Summary ``D+H`` Engineering line when VAPT sits in columns F–I).

Produces a single merged PNG of four cumulative portfolio panels and optionally uploads it to Slack.

Non-secret defaults (titles, DATE_START, etc.) use module constants below. Required settings use environment
variables (and optional `.env`): ``GOOGLE_SERVICE_ACCOUNT_JSON``, ``GOOGLE_SPREADSHEET_ID``, and optionally
Slack ``SLACK_BOT_TOKEN`` / ``SLACK_CHANNEL_ID``. See README in the dashboard repo.

**Optional Jira breached-fix latency (Slack only):** ``avg`` resolution age and ``avg`` days past SLA for breaches
identified by SLA **calendar math** (not PNG). Requires ``JIRA_BASE_URL``, ``JIRA_EMAIL``, ``JIRA_API_TOKEN`` unless
``COMPLIANCE_CHARTS_SKIP_JIRA`` is truthy; cohort resolved on or after ``DATE_START``, end date via
``COMPLIANCE_CHARTS_JIRA_RESOLVED_END`` (``sheet`` vs ``today``). Same behavior as ``compliance_workbook_charts.py``.
``requests``, plus **repository Python modules** next to this script (not on PyPI):
``jira_compliance_daily.py``, ``jira_sla_breach_label.py``, and ``jira_metrics_queries.json``.
Copy them from the metrics repo or add a submodule; CI must place them on ``PYTHONPATH`` / same directory as this script.

Depends: pandas, gspread, google-auth, matplotlib, numpy; slack_sdk for Slack chart upload + Block Kit summary.
``requests>=2.31`` when Jira metrics are enabled.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from jira_compliance_daily import BreachLatencyPortfolioMetrics

import numpy as np
import pandas as pd


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv

        root = Path(__file__).resolve().parent
        load_dotenv(root / ".env")
        load_dotenv()
    except ImportError:
        pass


def credentials_path_from_env() -> Path:
    """Resolve path to the Google service account JSON (local file)."""
    _load_dotenv_if_present()
    raw = (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    )
    if not raw:
        raise SystemExit(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS to the service account JSON path."
        )
    p = Path(raw).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"Service account JSON not found: {p}")
    return p


def spreadsheet_id_from_env() -> str:
    _load_dotenv_if_present()
    sid = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    if not sid:
        raise SystemExit("Set GOOGLE_SPREADSHEET_ID to the Google Sheet document ID (from the sheet URL).")
    return sid


def slack_tokens_from_env() -> Tuple[str, str]:
    """Return (bot_token, channel_id); both empty means skip Slack."""
    _load_dotenv_if_present()
    return (
        os.environ.get("SLACK_BOT_TOKEN", "").strip(),
        os.environ.get("SLACK_CHANNEL_ID", "").strip(),
    )

_GSHEETS_SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)

# Y–AH on each project tab: same semantics as A–K but without Daily-Compliance (10 columns).
DISPLAY_COLUMNS: Sequence[str] = (
    "Project",
    "Date",
    "Fixed-Total",
    "Fixed-Breached",
    "Fixed-Within_SLA",
    "Open-Breached",
    "Cumulative-Fixed-Total",
    "Cumulative-Fixed-Breached",
    "Cumulative-Fixed-Within_SLA",
    "Cumulative-Compliance",
)

# Summary + non-project placeholders (21 sheets → 19 project tabs when these are skipped).
DEFAULT_EXCLUDE = frozenset({"Summary", "NotPartOfScan"})

# =============================================================================
# Configuration — secrets via env / .env (see README).
# =============================================================================

_load_dotenv_if_present()

OUTPUT_PNG_PATH = Path(
    os.environ.get("OUTPUT_PNG_PATH", "cs-engg-security-compliance.png")
)

# First calendar date shown on charts (YYYY-MM-DD). Same inclusive lower bound for optional **Jira breach**
# cohort averages (resolved ≥ DATE_START). Override via env DATE_START.
# Portfolio cumulative totals still aggregate from **all** sheet rows before clipping for display.
DATE_START = (os.environ.get("DATE_START", "2026-02-03").strip() or "2026-02-03")

# Extra sheet names to exclude beyond DEFAULT_EXCLUDE (always skipped).
EXTRA_EXCLUDE_SHEETS: Tuple[str, ...] = ()

# If non-empty, only load these sheet / project names; portfolio aggregates that subset only.
ONLY_PROJECT_SHEETS: Tuple[str, ...] = ()

CHART_TITLE = "CS Engineering - Security SLA compliance"

# Merged PNG layout (matplotlib).
FIGURE_SIZE_INCHES: Tuple[float, float] = (15.0, 10.0)
FIGURE_FACE_COLOR = "#f8f9fa"
SAVE_DPI = 200
LAST_POINT_LABEL_FONTSIZE = 10

# Block Kit header (shown above the metrics section).
SLACK_MESSAGE_HEADER = "CS Engineering - Security SLA compliance"
# Notification preview / accessibility fallback when blocks cannot render.
SLACK_MESSAGE_FALLBACK_PREFIX = "CS Engineering - Security SLA compliance"

# Optional Slack mrkdwn links (<url|label>) for each metric; leave "" for plain values.
SLACK_LINK_SLA_COMPLIANCE = ""
SLACK_LINK_CUMULATIVE_FIXES = ""
SLACK_LINK_OPEN_PAST_SLA = ""
SLACK_LINK_CUM_WITHIN_SLA = ""
SLACK_LINK_CUM_BREACHED = ""

SLACK_LINK_PER_PROJECT_DETAILS = os.environ.get("SLACK_LINK_PER_PROJECT_DETAILS", "").strip()
if not SLACK_LINK_PER_PROJECT_DETAILS:
    SLACK_LINK_PER_PROJECT_DETAILS = (
        "https://docs.google.com/spreadsheets/d/16pqLCcnWoLPvAUq6sNJCsXyc75mWHsBRcKcHH-d_oOc/edit?gid=1126363028#gid=1126363028"
    )


# Fiscal year label drawn at the figure’s top-left (override with CHART_FY_LABEL in .env).
CHART_FY_LABEL = os.environ.get("CHART_FY_LABEL", "FY2026-27").strip()

# Inclusive resolved-date lower bound for Jira breach averages — same as DATE_START.
JIRA_BREACH_METRICS_RESOLVED_START = DATE_START


class JiraBreachOverlay(NamedTuple):
    """Jira breach latency metrics plus resolved cohort window (Slack attachment text only — not drawn on PNG)."""

    metrics: "BreachLatencyPortfolioMetrics"
    resolved_start_inclusive: str
    resolved_end_inclusive: str


def _hint_missing_jira_helpers(script_dir: Path) -> None:
    """Print FOUND/MISSING for files CI often forgets to vendor beside this script."""
    names = (
        "jira_compliance_daily.py",
        "jira_sla_breach_label.py",
        "jira_metrics_queries.json",
    )
    print("  Presence beside compliance script:", file=sys.stderr, flush=True)
    for name in names:
        path = script_dir / name
        status = "FOUND " if path.is_file() else "MISSING"
        print(f"    [{status}] {path}", file=sys.stderr, flush=True)


def _jira_breach_slack_lines(ov: JiraBreachOverlay) -> List[str]:
    """Slack-only lines; same mrkdwn style as cumulative KPI rows."""
    jb = ov.metrics
    if jb.avg_resolution_age_days_breached is None or jb.avg_days_past_sla_at_resolution is None:
        return []
    approx_ar = int(round(jb.avg_resolution_age_days_breached))
    approx_ap = int(round(jb.avg_days_past_sla_at_resolution))
    return [
        f"*Avg resolution age:* {approx_ar:,} days\n",
        f"*Avg past SLA:* {approx_ap:,} days\n",
    ]


def try_fetch_jira_breach_latency_metrics(
    *,
    sheet_resolved_end_inclusive: Optional[date] = None,
) -> Optional[JiraBreachOverlay]:
    """
    Pull resolved Jira issues (SLA breach = calendar math; ``sla-breached`` label ignored for classification).
    Used for Slack message only — chart PNG is unchanged.
    """
    _load_dotenv_if_present()
    skip = os.environ.get("COMPLIANCE_CHARTS_SKIP_JIRA", "").strip().lower()
    if skip in ("1", "true", "yes"):
        print(
            "Note: Jira breach latency (Slack) disabled (COMPLIANCE_CHARTS_SKIP_JIRA).",
            flush=True,
        )
        return None

    base = (os.environ.get("JIRA_BASE_URL") or "").strip().rstrip("/")
    email = (os.environ.get("JIRA_EMAIL") or "").strip().strip('"').strip("'")
    raw_tok = os.environ.get("JIRA_API_TOKEN") or ""
    token = raw_tok.replace("\ufeff", "").strip().strip('"').strip("'")
    if not base or not email or not token:
        missing = [
            label
            for label, ok in (
                ("JIRA_BASE_URL", bool(base)),
                ("JIRA_EMAIL", bool(email)),
                ("JIRA_API_TOKEN", bool(token)),
            )
            if not ok
        ]
        print(
            "Note: Jira breach latency skipped — these env vars are empty or unset: "
            + ", ".join(missing)
            + ". Slack message will omit Avg resolution age / Avg past SLA.",
            flush=True,
        )
        if os.environ.get("GITHUB_ACTIONS", "").strip().lower() in ("true", "1"):
            print(
                "GitHub Actions: add a step `env:` mapping exactly those names from Repository secrets "
                "(e.g. JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}); secret names alone are not enough.",
                flush=True,
            )
        return None

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        print(
            "Warning: zoneinfo unavailable; skip Jira breach latency.",
            file=sys.stderr,
        )
        return None

    tz_name = (os.environ.get("JIRA_COMPLIANCE_TIMEZONE") or "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        print(
            f"Warning: invalid JIRA_COMPLIANCE_TIMEZONE {tz_name!r} ({e}); skip Jira metrics.",
            file=sys.stderr,
        )
        return None

    try:
        start_d = date.fromisoformat(JIRA_BREACH_METRICS_RESOLVED_START.strip())
    except ValueError:
        print(
            f"Warning: invalid DATE_START / Jira cohort {JIRA_BREACH_METRICS_RESOLVED_START!r}; skip.",
            file=sys.stderr,
        )
        return None

    today_d = datetime.now(tz).date()
    end_mode = (
        os.environ.get("COMPLIANCE_CHARTS_JIRA_RESOLVED_END", "sheet") or "sheet"
    ).strip().lower()
    if end_mode in ("today", "now"):
        end_d = today_d
    elif sheet_resolved_end_inclusive is not None:
        end_d = min(today_d, sheet_resolved_end_inclusive)
        if end_d < today_d:
            print(
                f"Note: Jira breach cohort capped at Sheet max resolved date {end_d} ({tz_name}); "
                f"today is {today_d}. Set COMPLIANCE_CHARTS_JIRA_RESOLVED_END=today for newer resolutions.",
                flush=True,
            )
    else:
        end_d = today_d

    if end_d < start_d:
        print(
            "Warning: Jira resolved cohort end before DATE_START; skip Jira metrics.",
            file=sys.stderr,
        )
        return None

    try:
        import requests  # noqa: F401 — verify pip dependency before local imports
    except ImportError as e:
        print(
            "Warning: package `requests` is not installed — Jira breach latency skipped.\n"
            f"  ImportError: {e}\n"
            "  Fix: add `requests>=2.31` to your workflow pip install / requirements.txt.",
            file=sys.stderr,
            flush=True,
        )
        return None

    script_dir = Path(__file__).resolve().parent
    try:
        from jira_compliance_daily import (
            DEFAULT_METRICS_JSON,
            build_jql,
            build_probe_jql,
            compute_breach_latency_portfolio_metrics,
            fetch_all_issues_jql,
            jira_session,
            resolve_project_filter,
        )
        from jira_sla_breach_label import try_resolve_custom_field_ids
    except ImportError as e:
        print(
            "Warning: Jira breach latency skipped — cannot import local helper modules.\n"
            f"  ImportError: {e}\n"
            "  Cause: `jira_compliance_daily` / `jira_sla_breach_label` are **not pip packages**.\n"
            f"  This script runs from: {script_dir}\n"
            "  Required files **in that directory** (or on PYTHONPATH):\n"
            "    - jira_compliance_daily.py\n"
            "    - jira_sla_breach_label.py\n"
            "    - jira_metrics_queries.json (default scope; override with JIRA_METRICS_JSON)\n"
            "  CI fix: copy those files from your metrics repo into the dashboard checkout, "
            "or checkout the repo as a submodule / second clone step.\n"
            "  Pip is separate: ensure `requests>=2.31` is installed.",
            file=sys.stderr,
            flush=True,
        )
        if os.environ.get("GITHUB_ACTIONS", "").strip().lower() in ("true", "1"):
            print(
                "  GitHub Actions: verify the workflow checks out files alongside "
                "`compliance_workbook_charts_git.py` (default branch contains them).",
                file=sys.stderr,
                flush=True,
            )
        _hint_missing_jira_helpers(script_dir)
        return None

    mj_raw = os.environ.get("JIRA_METRICS_JSON", "").strip()
    mj = Path(mj_raw).expanduser().resolve() if mj_raw else DEFAULT_METRICS_JSON
    try:
        with open(mj, encoding="utf-8") as f:
            scope_data = json.load(f)
        base_scope = (scope_data.get("base_scope") or "").strip()
        if not base_scope:
            print(f"Warning: base_scope missing in {mj}; skip Jira.", file=sys.stderr)
            return None
    except OSError as e:
        print(f"Warning: cannot read JIRA metrics JSON {mj}: {e}", file=sys.stderr)
        return None

    if ONLY_PROJECT_SHEETS:
        project_filter = {str(x).strip().upper() for x in ONLY_PROJECT_SHEETS if str(x).strip()}
        if not project_filter:
            project_filter = None
    else:
        project_filter = resolve_project_filter(None, None)

    resolved_lt = end_d + timedelta(days=1)
    jql = build_jql(base_scope, start_d, resolved_lt, project_filter)

    try:
        max_results = int(
            os.environ.get("COMPLIANCE_CHARTS_JIRA_MAX_RESULTS", "100").strip() or "100"
        )
    except ValueError:
        print(
            "Warning: invalid COMPLIANCE_CHARTS_JIRA_MAX_RESULTS; skip Jira.",
            file=sys.stderr,
        )
        return None

    try:
        session = jira_session(base, email, token)
        probe_jql = build_probe_jql(project_filter, base_scope)
        pair = try_resolve_custom_field_ids(session, base, probe_jql)
        if pair is None:
            print(
                "Warning: Jira Severity / vulnerability_introduced_date lookup failed; "
                "Slack breach averages omitted.",
                file=sys.stderr,
                flush=True,
            )
            return None
        severity_id, introduced_id = pair
        issue_fields = ["key", "project", "labels", "resolutiondate", severity_id, introduced_id]
        issues = fetch_all_issues_jql(
            session, base, jql, max_results=max_results, fields=issue_fields
        )
        print(f"Jira breach cohort fetch: {len(issues):,} resolved issues (JQL window).", flush=True)
        if len(issues) == 0:
            print(
                "Hint: 0 issues — widen filters/dates or check token scope.\n" f"JQL:\n{jql}",
                file=sys.stderr,
                flush=True,
            )
        metrics = compute_breach_latency_portfolio_metrics(
            issues,
            breach_by_label=False,
            severity_id=severity_id,
            introduced_id=introduced_id,
            label_fallback=False,
        )
        print(
            f"Jira breach cohort (resolved {start_d.isoformat()}–{end_d.isoformat()}, SLA calendar math): "
            f"{metrics.breached_issue_count:,} breached fixes; "
            f"{metrics.breached_with_latency_days_count:,} with latency inputs.",
            flush=True,
        )
        ages = metrics.avg_resolution_age_days_breached
        pasta = metrics.avg_days_past_sla_at_resolution
        if ages is not None and pasta is not None:
            print(
                "Slack message will include KPI lines: "
                f"Avg resolution age: {int(round(ages)):,} days; "
                f"Avg past SLA: {int(round(pasta)):,} days.",
                flush=True,
            )
        return JiraBreachOverlay(
            metrics=metrics,
            resolved_start_inclusive=start_d.isoformat(),
            resolved_end_inclusive=end_d.isoformat(),
        )
    except requests.RequestException as e:
        print(f"Warning: Jira API error ({e}); skip breach latency.", file=sys.stderr)
        return None


class ChartSeries(NamedTuple):
    dates: pd.Series
    compliance_pct: pd.Series
    cum_total: pd.Series
    open_breached: pd.Series
    cum_within: pd.Series
    cum_breached: pd.Series


def _parse_pct_cell(val: object) -> Optional[float]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _pad_row(row: Sequence[object], length: int) -> List[object]:
    r = list(row)
    if len(r) < length:
        r.extend([None] * (length - len(r)))
    return r[:length]


def load_project_frames(
    credentials_path: Path,
    spreadsheet_id: str,
    *,
    exclude_sheets: Iterable[str],
    only_projects: Optional[Sequence[str]],
) -> Tuple[pd.DataFrame, List[str]]:
    """Load all project tabs from a Google Sheet (columns Y–AH), mirroring the former Excel export.

    Keeps **all** dated rows so portfolio cumulative series matchesΣ per-sheet ``Cumulative-Fixed-*`` last row
    (= Summary totals). Slice to ``DATE_START`` in ``main()`` for charts only.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise SystemExit(
            "Google Sheets requires gspread and google-auth. pip install gspread google-auth"
        ) from e

    creds_path = credentials_path.expanduser().resolve()
    if not creds_path.is_file():
        raise SystemExit(f"Service account JSON not found: {creds_path}")

    creds = Credentials.from_service_account_file(
        str(creds_path), scopes=list(_GSHEETS_SCOPES)
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id.strip())

    exclude = set(exclude_sheets) | DEFAULT_EXCLUDE
    only_set = set(only_projects) if only_projects else None
    ncols = len(DISPLAY_COLUMNS)

    frames: List[pd.DataFrame] = []

    for worksheet in sh.worksheets():
        sheet_name = worksheet.title
        if sheet_name in exclude:
            continue
        if only_set is not None and sheet_name not in only_set:
            continue

        try:
            values = worksheet.get("Y:AH")
        except Exception as e:
            raise SystemExit(f"Failed to read range Y:AH from sheet {sheet_name!r}: {e}") from e

        if not values or len(values) < 2:
            continue

        data_rows = [_pad_row(r, ncols) for r in values[1:]]
        if not data_rows:
            continue

        df = pd.DataFrame(data_rows, columns=list(DISPLAY_COLUMNS))

        pk_series = df["Project"].astype(str).str.strip()
        mask_bad = pk_series.isna() | (pk_series == "") | (pk_series == "nan")
        df.loc[mask_bad, "Project"] = sheet_name

        df["_sheet"] = sheet_name
        frames.append(df)

    if not frames:
        raise SystemExit("No project sheets loaded after filters.")

    out = pd.concat(frames, ignore_index=True)
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")

    for col in (
        "Fixed-Total",
        "Fixed-Breached",
        "Fixed-Within_SLA",
        "Open-Breached",
        "Cumulative-Fixed-Total",
        "Cumulative-Fixed-Breached",
        "Cumulative-Fixed-Within_SLA",
    ):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["Cumulative-Compliance"] = out["Cumulative-Compliance"].map(_parse_pct_cell)

    out = out[out["Date"].notna()].copy()

    proj_keys = sorted(out["Project"].drop_duplicates().tolist(), key=str)
    return out, proj_keys


def dedupe_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Project", "Date"])
    return df.drop_duplicates(subset=["Project", "Date"], keep="last")


def compliance_pct_from_totals(cum_f: pd.Series, cum_b: pd.Series) -> pd.Series:
    """Align with JiraComplianceDaily.gs cdlCompliancePct_."""
    result = pd.Series(np.nan, index=cum_f.index, dtype="float64")
    mask = cum_f.notna() & (cum_f > 0)
    result.loc[mask] = np.round(10000 * (1 - cum_b.loc[mask] / cum_f.loc[mask])) / 100
    return result


def build_portfolio_series(df: pd.DataFrame) -> pd.DataFrame:
    """Sum daily C,D,E,G by Date; cumulative H,I,J,K from running totals."""
    d = dedupe_dates(df)
    daily = (
        d.groupby("Date", as_index=False)[
            ["Fixed-Total", "Fixed-Breached", "Fixed-Within_SLA", "Open-Breached"]
        ]
        .sum()
        .sort_values("Date")
    )
    cum_f = daily["Fixed-Total"].cumsum()
    cum_b = daily["Fixed-Breached"].cumsum()
    daily = daily.assign(
        **{
            "Cumulative-Fixed-Total": cum_f,
            "Cumulative-Fixed-Breached": cum_b,
            "Cumulative-Fixed-Within_SLA": cum_f - cum_b,
        }
    )
    daily["Cumulative-Compliance"] = compliance_pct_from_totals(cum_f, cum_b)
    return daily


def chart_series_from_portfolio_df(pf: pd.DataFrame) -> ChartSeries:
    return ChartSeries(
        dates=pf["Date"],
        compliance_pct=pf["Cumulative-Compliance"],
        cum_total=pf["Cumulative-Fixed-Total"],
        open_breached=pf["Open-Breached"],
        cum_within=pf["Cumulative-Fixed-Within_SLA"],
        cum_breached=pf["Cumulative-Fixed-Breached"],
    )


def latest_portfolio_metrics(portfolio_df: pd.DataFrame) -> Dict[str, str]:
    """Latest portfolio row (same endpoints as chart annotations)."""
    if portfolio_df.empty:
        raise SystemExit("Portfolio aggregate has no rows.")
    last = portfolio_df.sort_values("Date").iloc[-1]
    date_str = pd.Timestamp(last["Date"]).strftime("%b %d, %Y")

    comp = last["Cumulative-Compliance"]
    compliance_str = f"{float(comp):.1f}%" if pd.notna(comp) else "—"

    def _count(col: str) -> str:
        v = last[col]
        return f"{int(round(float(v))):,}" if pd.notna(v) else "—"

    return {
        "date": date_str,
        "sla_compliance_pct": compliance_str,
        "cumulative_fixes": _count("Cumulative-Fixed-Total"),
        "open_past_sla": _count("Open-Breached"),
        "cum_within_sla": _count("Cumulative-Fixed-Within_SLA"),
        "cum_breached": _count("Cumulative-Fixed-Breached"),
    }


def _slack_link_or_plain(link_url: str, display: str) -> str:
    u = link_url.strip()
    if u:
        return f"<{u}|{display}>"
    return display


def build_slack_blocks(
    metrics: Dict[str, str],
    jira_overlay: Optional[JiraBreachOverlay] = None,
) -> List[dict]:
    """Header + section mrkdwn mirroring sheet chart values; optional Jira breach timings (Slack only)."""
    lines: List[str] = []
    # intro = SLACK_UPLOAD_COMMENT.strip()
    # if intro:
    #     lines.append(intro + "\n\n")

    lines.append(f"*Date:* {metrics['date']}\n")
    lines.append(
        f"*SLA compliance rate:* {_slack_link_or_plain(SLACK_LINK_SLA_COMPLIANCE, metrics['sla_compliance_pct'])}\n"
    )
    lines.append(
        f"*Cumulative fixes completed:* {_slack_link_or_plain(SLACK_LINK_CUMULATIVE_FIXES, metrics['cumulative_fixes'])}\n"
    )
    lines.append(
        f"*Open issues past SLA deadline:* {_slack_link_or_plain(SLACK_LINK_OPEN_PAST_SLA, metrics['open_past_sla'])}\n"
    )
    lines.append(
        f"*Cumulative fixed — within SLA:* {_slack_link_or_plain(SLACK_LINK_CUM_WITHIN_SLA, metrics['cum_within_sla'])}\n"
    )
    lines.append(
        f"*Cumulative fixed — breached SLA:* {_slack_link_or_plain(SLACK_LINK_CUM_BREACHED, metrics['cum_breached'])}\n"
    )

    if jira_overlay is not None:
        j_extra = _jira_breach_slack_lines(jira_overlay)
        if j_extra:
            for block in j_extra:
                lines.append(block)

    detail_url = SLACK_LINK_PER_PROJECT_DETAILS.strip()
    if detail_url:
        lines.append(
            "\n"
            "*Per-project detail:* "
            f"<{detail_url}|Click here to view details for each project>.\n"
        )
    else:
        lines.append(
            "\n"
            "_Tip:_ Set `SLACK_LINK_PER_PROJECT_DETAILS` in this script to add a clickable link "
            "(workbook, Jira, or dashboard) for project-level numbers.\n"
        )

    text = "".join(lines)
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": SLACK_MESSAGE_HEADER,
                "emoji": True,
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


_PANEL_COLORS = {
    "compliance": "#0077b6",
    "fixed_total": "#e85d04",
    "open_breach": "#c1121f",
    "within_sla": "#2d6a4f",
    "breached": "#e63946",
}


def _last_finite_point(dates: pd.Series, vals: pd.Series) -> Tuple[Optional[pd.Timestamp], Optional[float]]:
    dt = pd.to_datetime(dates)
    y = pd.to_numeric(vals, errors="coerce").astype(float)
    ok = y.notna() & np.isfinite(y.to_numpy())
    if not ok.any():
        return None, None
    pos = np.flatnonzero(ok.to_numpy())[-1]
    return dt.iloc[pos], float(y.iloc[pos])


def _annotate_latest_on_line(
    ax,
    dates: pd.Series,
    vals: pd.Series,
    *,
    label_fmt: Callable[[float], str],
    color: str,
    xytext: Tuple[float, float] = (8.0, 8.0),
) -> None:
    """Label the latest finite point on a line (most recent date in the series)."""
    x, y = _last_finite_point(dates, vals)
    if x is None or y is None:
        return
    ax.annotate(
        label_fmt(y),
        xy=(x, y),
        xytext=xytext,
        textcoords="offset points",
        fontsize=LAST_POINT_LABEL_FONTSIZE,
        fontweight="bold",
        color=color,
        bbox=dict(
            boxstyle="round,pad=0.35",
            facecolor="white",
            edgecolor=color,
            linewidth=1.2,
            alpha=0.95,
        ),
        arrowprops=dict(arrowstyle="-", color=color, lw=0.75, alpha=0.65),
    )


def plot_portfolio_merged_png(portfolio: ChartSeries, output: Path, title: str) -> None:
    """Draw four cumulative portfolio panels in a 2×2 grid and save as one PNG."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    def _fmt_month_day(x: float, pos: object = None) -> str:
        """X tick label like 'Feb 6' (no leading zero on day)."""
        dt = mdates.num2date(x)
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
        return f"{dt.strftime('%b')} {dt.day}"

    c = _PANEL_COLORS
    fig, axes = plt.subplots(
        2,
        2,
        figsize=FIGURE_SIZE_INCHES,
        facecolor=FIGURE_FACE_COLOR,
    )
    ax_k, ax_h, ax_g, ax_ij = axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]
    d = portfolio.dates

    for ax in axes.flat:
        ax.set_facecolor("#ffffff")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, alpha=0.3, color="#e9ecef", linewidth=0.8)
        ax.tick_params(axis="x", rotation=30, labelsize=9)
        ax.tick_params(axis="y", labelsize=9)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(_fmt_month_day))

    k = portfolio.compliance_pct
    mask_k = k.notna()
    if mask_k.any():
        ax_k.plot(d[mask_k], k[mask_k], color=c["compliance"], linewidth=2.2)
    _annotate_latest_on_line(
        ax_k,
        d,
        portfolio.compliance_pct,
        label_fmt=lambda v: f"{v:.1f}%",
        color=c["compliance"],
        xytext=(8.0, 10.0),
    )
    ax_k.set_title("SLA Compliance Rate (%)", fontsize=12, fontweight="bold", pad=6)
    ax_k.set_ylabel("Compliance (%)", fontsize=10, color="#495057")
    ax_k.set_ylim(0.0, 100.0)
    ax_k.yaxis.set_major_locator(mticker.MultipleLocator(20))

    ax_k.text(
        0.01,
        -0.18,
        "% of fixes resolved within SLA",
        transform=ax_k.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    ax_h.plot(d, portfolio.cum_total, color=c["fixed_total"], linewidth=2.2)
    _annotate_latest_on_line(
        ax_h,
        d,
        portfolio.cum_total,
        label_fmt=lambda v: f"{int(round(v)):,.0f}",
        color=c["fixed_total"],
        xytext=(8.0, 8.0),
    )
    ax_h.set_title("Cumulative Fixes Completed", fontsize=12, fontweight="bold", pad=6)
    ax_h.set_ylabel("Issues (count)", fontsize=10, color="#495057")
    ax_h.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax_h.text(
        0.01,
        -0.18,
        "Running total of security issues resolved since Feb 2026",
        transform=ax_h.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    g = portfolio.open_breached
    mask_g = g.notna()
    if mask_g.any():
        ax_g.plot(d[mask_g], g[mask_g], color=c["open_breach"], linewidth=2.2)
    _annotate_latest_on_line(
        ax_g,
        d,
        portfolio.open_breached,
        label_fmt=lambda v: f"{int(round(v)):,.0f}",
        color=c["open_breach"],
        xytext=(8.0, 8.0),
    )
    ax_g.set_title("Open Issues Past SLA Deadline", fontsize=12, fontweight="bold", pad=6)
    ax_g.set_ylabel("Issues (count)", fontsize=10, color="#495057")
    ax_g.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax_g.text(
        0.01,
        -0.18,
        "Active issues already past their SLA deadline",
        transform=ax_g.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    ax_ij.plot(d, portfolio.cum_within, label="Within SLA", color=c["within_sla"], linewidth=2.2)
    ax_ij.plot(
        d,
        portfolio.cum_breached,
        label="SLA Breached",
        color=c["breached"],
        linewidth=2.2,
        linestyle="--",
    )
    _annotate_latest_on_line(
        ax_ij,
        d,
        portfolio.cum_within,
        label_fmt=lambda v: f"Within {int(round(v)):,.0f}",
        color=c["within_sla"],
        xytext=(8.0, 12.0),
    )
    _annotate_latest_on_line(
        ax_ij,
        d,
        portfolio.cum_breached,
        label_fmt=lambda v: f"Breached {int(round(v)):,.0f}",
        color=c["breached"],
        xytext=(8.0, -14.0),
    )
    ax_ij.set_title("Fixed Issues: Within SLA vs Breached", fontsize=12, fontweight="bold", pad=6)
    ax_ij.set_ylabel("Issues (count)", fontsize=10, color="#495057")
    ax_ij.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax_ij.legend(loc="upper left", fontsize=9, framealpha=0.8)
    ax_ij.text(
        0.01,
        -0.18,
        "Cumulative split by outcome (solid = within, dashed = breached)",
        transform=ax_ij.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    fig.tight_layout(
        pad=0.55,
        h_pad=1.05,
        w_pad=1.05,
        rect=[0.02, 0.038, 0.98, 0.965],
    )
    fig.suptitle(title, fontsize=14, fontweight="bold", color="#1a1a2e", y=0.991)
    if CHART_FY_LABEL:
        fig.text(
            0.02,
            0.986,
            CHART_FY_LABEL,
            transform=fig.transFigure,
            ha="left",
            va="top",
            fontsize=10,
            fontweight="bold",
            color="#495057",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output,
        dpi=SAVE_DPI,
        bbox_inches="tight",
        pad_inches=0.12,
        facecolor=fig.get_facecolor(),
    )
    plt.close(fig)


def post_compliance_to_slack(
    image_path: Path,
    portfolio_df: pd.DataFrame,
    *,
    token: str,
    channel_id: str,
    jira_overlay: Optional[JiraBreachOverlay] = None,
) -> None:
    """Upload PNG with ``files_upload_v2``, then send Block Kit summary."""
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as e:
        raise SystemExit(
            "Slack integration requires slack-sdk. pip install slack-sdk"
        ) from e

    metrics = latest_portfolio_metrics(portfolio_df)
    blocks = build_slack_blocks(metrics, jira_overlay=jira_overlay)
    fallback = f"{SLACK_MESSAGE_FALLBACK_PREFIX} — {metrics['date']}"

    client = WebClient(token=token.strip())
    channel_b = channel_id.strip()

    try:
        client.files_upload_v2(
            channel=channel_b,
            file=str(image_path.resolve()),
            title=image_path.name,
        )
    except SlackApiError as e:
        err = e.response.get("error") if e.response else str(e)
        raise SystemExit(f"Slack files_upload_v2 failed: {err}") from e

    try:
        client.chat_postMessage(
            channel=channel_b,
            text=fallback,
            blocks=blocks,
        )
    except SlackApiError as e:
        err = e.response.get("error") if e.response else str(e)
        raise SystemExit(f"Slack chat_postMessage failed: {err}") from e

    print(f"Posted chart image + summary message to Slack channel {channel_b}")


def main() -> None:
    cred_path = credentials_path_from_env()
    sheet_id = spreadsheet_id_from_env()
    exclude_extra = frozenset(EXTRA_EXCLUDE_SHEETS)
    only = list(ONLY_PROJECT_SHEETS) if ONLY_PROJECT_SHEETS else None

    df, _keys = load_project_frames(
        cred_path,
        sheet_id,
        exclude_sheets=exclude_extra,
        only_projects=only,
    )

    portfolio_full = build_portfolio_series(df)
    start_dt = pd.to_datetime(DATE_START)
    portfolio_df = portfolio_full[portfolio_full["Date"] >= start_dt].copy()
    if portfolio_df.empty:
        raise SystemExit(
            f"No portfolio rows on or after DATE_START={DATE_START!r}. "
            "Lower DATE_START or check sheet date cells."
        )
    portfolio = chart_series_from_portfolio_df(portfolio_df)

    sheet_end = pd.Timestamp(portfolio_full["Date"].max()).date()
    jira_overlay = try_fetch_jira_breach_latency_metrics(
        sheet_resolved_end_inclusive=sheet_end,
    )

    out = OUTPUT_PNG_PATH.resolve()
    plot_portfolio_merged_png(portfolio, out, CHART_TITLE)
    print(f"Wrote {out}")

    slack_token, slack_channel = slack_tokens_from_env()
    if slack_token and slack_channel:
        post_compliance_to_slack(
            out,
            portfolio_df,
            token=slack_token,
            channel_id=slack_channel,
            jira_overlay=jira_overlay,
        )
    elif slack_token or slack_channel:
        raise SystemExit(
            "Set both SLACK_BOT_TOKEN and SLACK_CHANNEL_ID (or leave both unset) to skip Slack."
        )


if __name__ == "__main__":
    main()
