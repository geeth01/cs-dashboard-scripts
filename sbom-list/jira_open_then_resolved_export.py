#!/usr/bin/env python3
"""
Export issues that were **not Done** (open / in-flight) on a given calendar snapshot
date and were **resolved later** — using Jira history JQL ``status WAS NOT IN (...) ON``.

This answers: “Which tickets were still open on day D but closed afterward?” It is
**not** the same as the compliance CSV (which counts issues **resolved on** day D).

Requires Jira Cloud/DC support for historical ``WAS`` / ``ON`` JQL (validated on your
site). Done-status names default to Done, Archived, Rejected (same idea as backlog
queries in jira_metrics_queries.json); override with ``--done-statuses`` if your
workflow uses different terminal statuses.

Example::

  python3 jira_open_then_resolved_export.py \\
    --start 2026-02-01 --end 2026-02-26 \\
    --projects OAA \\
    -o oaa_open_then_closed.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

LOGGER = logging.getLogger("jira_open_then_resolved_export")

SLA_LABEL = "sla-breached"
DEFAULT_METRICS_JSON = Path(__file__).resolve().parent / "jira_metrics_queries.json"


def load_config() -> Tuple[str, str, str]:
    _root = Path(__file__).resolve().parent
    load_dotenv(_root / ".env")
    load_dotenv()
    base = (os.environ.get("JIRA_BASE_URL") or "").strip().rstrip("/")
    email = (os.environ.get("JIRA_EMAIL") or "").strip().strip('"').strip("'")
    raw = os.environ.get("JIRA_API_TOKEN") or ""
    token = raw.replace("\ufeff", "").strip().strip('"').strip("'")
    if not base or not email or not token:
        LOGGER.error(
            "Set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN in the environment or .env"
        )
        sys.exit(1)
    return base, email, token


def jira_session(base: str, email: str, token: str) -> requests.Session:
    s = requests.Session()
    s.auth = (email, token)
    s.headers.update(
        {"Accept": "application/json", "Content-Type": "application/json"}
    )
    return s


def load_base_scope(metrics_path: Path) -> str:
    with open(metrics_path, encoding="utf-8") as f:
        data = json.load(f)
    scope = (data.get("base_scope") or "").strip()
    if not scope:
        LOGGER.error("base_scope missing in %s", metrics_path)
        sys.exit(1)
    return scope


def parse_project_list(s: Optional[str]) -> Optional[Set[str]]:
    if not s or not s.strip():
        return None
    return {p.strip().upper() for p in s.split(",") if p.strip()}


def parse_done_statuses(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def status_open_on_snapshot_jql(done_statuses: List[str], as_of: date) -> str:
    """Single JQL fragment: status WAS NOT IN (...) ON (YYYY-MM-DD)."""
    inner = ", ".join(done_statuses)
    ds = f"{as_of.year}-{as_of.month:02d}-{as_of.day:02d}"
    return f"status WAS NOT IN ({inner}) ON ({ds})"


def build_jql(
    base_scope: str,
    as_of: date,
    resolved_not_before: date,
    project_keys: Optional[Set[str]],
    done_statuses: List[str],
    only_sla_breached: bool,
) -> str:
    """
    Issues that were not in a Done-like status on ``as_of`` and have resolution
    on or after ``resolved_not_before`` (typically the next calendar day after as_of).
    """
    parts = [
        "(" + base_scope + ")",
        '(labels = "vulnerability/sca" OR labels = "vulnerability/sast")',
        'labels = "vulnerability/fixable"',
        'labels != "vulnerability/ignored"',
    ]
    if project_keys:
        parts.append("project in (" + ", ".join(sorted(project_keys)) + ")")
    # Historical snapshot (Jira evaluates ON (date) in site timezone)
    parts.append(status_open_on_snapshot_jql(done_statuses, as_of))
    parts.append("resolution IS NOT EMPTY")
    rb = resolved_not_before.strftime("%Y-%m-%d")
    parts.append(f'resolved >= "{rb}"')
    if only_sla_breached:
        parts.append(f'labels = "{SLA_LABEL}"')
    return " AND ".join(parts)


def search_jql_page(
    session: requests.Session,
    base: str,
    jql: str,
    max_results: int,
    next_page_token: Optional[str],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["key", "project", "summary", "labels", "resolutiondate", "status"],
    }
    if next_page_token:
        body["nextPageToken"] = next_page_token
    r = session.post(f"{base}/rest/api/3/search/jql", json=body)
    r.raise_for_status()
    return r.json()


def project_key(issue: Dict[str, Any]) -> str:
    proj = (issue.get("fields") or {}).get("project")
    if isinstance(proj, dict) and proj.get("key"):
        return str(proj["key"])
    return ""


def issue_rows(
    issues: List[Dict[str, Any]],
    as_of: date,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for issue in issues:
        fields = issue.get("fields") or {}
        labels = fields.get("labels") or []
        res = fields.get("resolutiondate")
        rows.append(
            {
                "AsOfDate": as_of.isoformat(),
                "Project": project_key(issue),
                "Issue": issue.get("key") or "",
                "Summary": (fields.get("summary") or "").replace("\n", " "),
                "Resolved": str(res) if res else "",
                "SLA_Breached_Label": "yes" if SLA_LABEL in labels else "no",
                "Labels": " ".join(sorted(labels)),
            }
        )
    rows.sort(key=lambda r: (r["Project"], r["Issue"]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export issues open (not Done) on AsOfDate but resolved later (historical JQL)."
    )
    parser.add_argument("--start", required=True, help="First snapshot date (inclusive) YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Last snapshot date (inclusive) YYYY-MM-DD")
    parser.add_argument(
        "--resolved-after",
        choices=("next_day", "same_day"),
        default="next_day",
        help="Require resolved on/after next calendar day after AsOfDate (default), "
        "or allow same-day resolution (default next_day matches 'later' in the same day edge case)",
    )
    parser.add_argument(
        "--only-sla-breached",
        action="store_true",
        help="Only include issues that have the sla-breached label when closed",
    )
    parser.add_argument(
        "--done-statuses",
        default="Done, Archived, Rejected",
        help="Comma-separated terminal status names excluded from 'open' on snapshot day",
    )
    parser.add_argument("--projects", default=None, help="Comma-separated project keys")
    parser.add_argument(
        "--metrics-json",
        type=Path,
        default=DEFAULT_METRICS_JSON,
    )
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output CSV path")
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="Document only: Jira evaluates ON (date) in the Jira site timezone; "
        "set your reporting zone to match Issue Navigator (not used in API math here).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if ZoneInfo is None:
        LOGGER.error("Python 3.9+ required.")
        sys.exit(1)

    try:
        start_d = date.fromisoformat(args.start.strip())
        end_d = date.fromisoformat(args.end.strip())
    except ValueError as e:
        LOGGER.error("Invalid date: %s", e)
        sys.exit(1)
    if end_d < start_d:
        LOGGER.error("--end before --start")
        sys.exit(1)

    done_list = parse_done_statuses(args.done_statuses)
    if not done_list:
        LOGGER.error("--done-statuses cannot be empty")
        sys.exit(1)

    base_scope = load_base_scope(args.metrics_json)
    project_filter = parse_project_list(args.projects)

    base, email, token = load_config()
    session = jira_session(base, email, token)

    all_rows: List[Dict[str, str]] = []
    d = start_d
    while d <= end_d:
        if args.resolved_after == "next_day":
            resolved_not_before = d + timedelta(days=1)
        else:
            resolved_not_before = d
        jql = build_jql(
            base_scope,
            d,
            resolved_not_before,
            project_filter,
            done_list,
            args.only_sla_breached,
        )
        LOGGER.debug("AsOf %s JQL: %s", d, jql)

        next_token: Optional[str] = None
        batch_issues: List[Dict[str, Any]] = []
        while True:
            data = search_jql_page(session, base, jql, args.max_results, next_token)
            batch = data.get("issues") or []
            batch_issues.extend(batch)
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token:
                break

        LOGGER.info("AsOf %s: %s issues", d, len(batch_issues))
        all_rows.extend(issue_rows(batch_issues, d))
        d += timedelta(days=1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "AsOfDate",
        "Project",
        "Issue",
        "Summary",
        "Resolved",
        "SLA_Breached_Label",
        "Labels",
    ]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in all_rows:
            w.writerow(row)

    LOGGER.info("Wrote %s rows to %s", len(all_rows), args.output)


if __name__ == "__main__":
    main()
