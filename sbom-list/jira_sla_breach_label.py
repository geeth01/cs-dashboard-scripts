#!/usr/bin/env python3
"""
Add Jira label ``sla-breached`` for Done OAA SCA issues fixed after the SLA due date.

Uses ``fields.resolutiondate`` as the moment the issue was resolved (typically when it
moved to Done). If your workflow sets resolution without aligning to Done, extend this
script to call ``GET /rest/api/3/issue/{key}/changelog`` and detect the transition into
status Done instead.

SLA: calendar days from ``vulnerability_introduced_date`` — Sev-0: 14, Sev-1: 30,
Sev-2: 90, Sev-3: 180. Breach if the resolution *date* (UTC) is strictly after
``introduced_date + N days``.

Uses Jira Cloud ``POST /rest/api/3/search/jql`` (``/rest/api/3/search`` is removed).
Optional env: ``JIRA_FIELD_SEVERITY`` and ``JIRA_FIELD_VULNERABILITY_INTRODUCED`` as
custom field ids if field-name discovery fails or multiple ``Severity`` fields exist.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

LOGGER = logging.getLogger("jira_sla_breach_label")

DEFAULT_JQL = (
    'labels in ("vulnerability/sca", "vulnerability/sast") AND labels = "vulnerability/fixable" '
    'AND labels != "vulnerability/ignored" AND project = OAA AND status = Done'
)

SLA_LABEL = "sla-breached"

SEVERITY_SLA_DAYS = {
    "Sev-0": 14,
    "Sev-1": 30,
    "Sev-2": 90,
    "Sev-3": 180,
}

# REST API field names (JQL UI names like Severity[Dropdown] differ — see resolve_* below)
INTRODUCED_FIELD_NAMES = ("vulnerability_introduced_date", "vulnerability_introduced_date[Date]")


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
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return s


def fetch_fields(session: requests.Session, base: str) -> List[Dict[str, Any]]:
    r = session.get(f"{base}/rest/api/3/field")
    r.raise_for_status()
    return r.json()


def _field_id_by_exact_name(
    fields: List[Dict[str, Any]], names: Tuple[str, ...]
) -> Optional[str]:
    by_name = {f.get("name"): f.get("id") for f in fields if f.get("name")}
    for n in names:
        if n in by_name:
            return str(by_name[n])
    return None


def _severity_field_candidates(fields: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for f in fields:
        if f.get("name") != "Severity":
            continue
        sch = f.get("schema") or {}
        if sch.get("type") == "option":
            fid = f.get("id")
            if fid:
                out.append(str(fid))
    return out


def probe_severity_field_id(
    session: requests.Session,
    base: str,
    jql: str,
    candidate_ids: List[str],
) -> Optional[str]:
    """Return the Severity field id that holds Sev-* on a matching issue, if any."""
    if not candidate_ids:
        return None
    body: Dict[str, Any] = {
        "jql": jql,
        "maxResults": 25,
        "fields": candidate_ids,
    }
    r = session.post(f"{base}/rest/api/3/search/jql", json=body)
    r.raise_for_status()
    data = r.json()
    for issue in data.get("issues") or []:
        flds = issue.get("fields") or {}
        for sid in candidate_ids:
            v = parse_severity_value(flds.get(sid))
            if v and v in SEVERITY_SLA_DAYS:
                return sid
    return None


def try_resolve_custom_field_ids(
    session: requests.Session, base: str, probe_jql: str
) -> Optional[Tuple[str, str]]:
    """
    Return (severity_field_id, introduced_field_id), or None if discovery fails.

    Used when callers must tolerate failure (e.g. optional chart overlays).
    CLI scripts should use resolve_custom_field_ids() which exits on failure.
    """
    env_sev = (os.environ.get("JIRA_FIELD_SEVERITY") or "").strip()
    env_intro = (os.environ.get("JIRA_FIELD_VULNERABILITY_INTRODUCED") or "").strip()

    all_fields = fetch_fields(session, base)

    introduced_id: Optional[str] = None
    if env_intro:
        introduced_id = env_intro
    else:
        introduced_id = _field_id_by_exact_name(all_fields, INTRODUCED_FIELD_NAMES)
    if not introduced_id:
        LOGGER.warning(
            "Could not find vulnerability_introduced_date field; skip optional Jira metrics. "
            "Set JIRA_FIELD_VULNERABILITY_INTRODUCED if needed."
        )
        return None

    severity_id: Optional[str] = None
    if env_sev:
        severity_id = env_sev
    else:
        candidates = _severity_field_candidates(all_fields)
        if len(candidates) == 1:
            severity_id = candidates[0]
        elif len(candidates) > 1:
            severity_id = probe_severity_field_id(session, base, probe_jql, candidates)
            if not severity_id:
                LOGGER.warning(
                    "Multiple Severity fields %s; could not pick one from sample issues; "
                    "skip optional Jira metrics. Set JIRA_FIELD_SEVERITY.",
                    candidates,
                )
                return None
        else:
            LOGGER.warning("No Severity select field found; skip optional Jira metrics.")
            return None

    LOGGER.debug("Using severity field %s, introduced field %s", severity_id, introduced_id)
    return severity_id, introduced_id


def resolve_custom_field_ids(
    session: requests.Session, base: str, probe_jql: str
) -> Tuple[str, str]:
    """Return (severity_field_id, introduced_field_id). Exit process if discovery fails."""
    pair = try_resolve_custom_field_ids(session, base, probe_jql)
    if pair is None:
        LOGGER.error(
            "Custom field resolution failed; set JIRA_FIELD_SEVERITY / "
            "JIRA_FIELD_VULNERABILITY_INTRODUCED or fix Jira field discovery."
        )
        sys.exit(1)
    return pair


def parse_severity_value(raw: Any) -> Optional[str]:
    """Normalize API option payload to canonical Sev-* keys in ``SEVERITY_SLA_DAYS`` when possible."""
    if raw is None:
        return None
    text: Optional[str] = None
    if isinstance(raw, str):
        text = raw.strip() or None
    elif isinstance(raw, dict):
        v = raw.get("value")
        if isinstance(v, str) and v.strip():
            text = v.strip()
        else:
            n = raw.get("name")
            if isinstance(n, str) and n.strip():
                text = n.strip()
    if not text:
        return None
    if text in SEVERITY_SLA_DAYS:
        return text
    for k in SEVERITY_SLA_DAYS:
        if k.lower() == text.lower():
            return k
    return text


def parse_introduced_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        inner = raw.get("value")
        if inner is None:
            inner = raw.get("date") or raw.get("iso8601")
        if inner is None:
            return None
        raw = inner
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        # "2024-01-15" or ISO datetime
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return date.fromisoformat(s[:10])
    return None


def parse_resolution_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).date()
        return date.fromisoformat(s[:10])
    return None


def sla_breached(
    introduced: date, severity: str, resolved: date
) -> Tuple[bool, Optional[int]]:
    n = SEVERITY_SLA_DAYS.get(severity)
    if n is None:
        return False, None
    due = introduced + timedelta(days=n)
    return resolved > due, n


PROBE_JQL_DEFAULT = (
    'project = SS AND labels = "vulnerability/sca" '
    'AND labels = "vulnerability/fixable" ORDER BY updated DESC'
)


def search_jql_page(
    session: requests.Session,
    base: str,
    jql: str,
    severity_id: str,
    introduced_id: str,
    max_results: int,
    next_page_token: Optional[str],
) -> Dict[str, Any]:
    fields = [
        "summary",
        "labels",
        "resolutiondate",
        severity_id,
        introduced_id,
    ]
    body: Dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": fields,
    }
    if next_page_token:
        body["nextPageToken"] = next_page_token
    r = session.post(f"{base}/rest/api/3/search/jql", json=body)
    r.raise_for_status()
    return r.json()


def add_label(
    session: requests.Session,
    base: str,
    issue_key: str,
    label: str,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True
    body = {"update": {"labels": [{"add": label}]}}
    r = session.put(f"{base}/rest/api/3/issue/{issue_key}", json=body)
    if not r.ok:
        LOGGER.error(
            "PUT %s failed: %s %s",
            issue_key,
            r.status_code,
            r.text[:500],
        )
        return False
    return True


def process_issue(
    issue: Dict[str, Any],
    severity_id: str,
    introduced_id: str,
    session: requests.Session,
    base: str,
    dry_run: bool,
    sleep_s: float,
) -> str:
    key = issue.get("key") or "?"
    fields = issue.get("fields") or {}
    labels = fields.get("labels") or []
    if SLA_LABEL in labels:
        return "skip_has_label"

    sev = parse_severity_value(fields.get(severity_id))
    if not sev or sev not in SEVERITY_SLA_DAYS:
        LOGGER.warning("%s: skip — severity missing or not Sev-0..Sev-3 (%r)", key, sev)
        return "skip_bad_severity"

    introduced = parse_introduced_date(fields.get(introduced_id))
    if introduced is None:
        LOGGER.warning("%s: skip — vulnerability_introduced_date empty", key)
        return "skip_no_introduced"

    resolved = parse_resolution_date(fields.get("resolutiondate"))
    if resolved is None:
        LOGGER.warning("%s: skip — resolutiondate empty", key)
        return "skip_no_resolution"

    breached, n = sla_breached(introduced, sev, resolved)
    if not breached:
        return "ok_within_sla"

    if add_label(session, base, key, SLA_LABEL, dry_run):
        if sleep_s > 0 and not dry_run:
            time.sleep(sleep_s)
        action = "would add" if dry_run else "added"
        LOGGER.info(
            "%s: %s %r (intro=%s sev=%s due=%s resolved=%s n=%s)",
            key,
            action,
            SLA_LABEL,
            introduced,
            sev,
            introduced + timedelta(days=n or 0),
            resolved,
            n,
        )
        return "labeled"
    return "error_update"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add sla-breached label to OAA Done SCA issues resolved after SLA due date."
    )
    parser.add_argument(
        "--jql",
        default=DEFAULT_JQL,
        help="JQL scope (default: OAA Done SCA fixable non-ignored)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform updates (default is dry-run only)",
    )
    parser.add_argument(
        "--next-page-token",
        default=None,
        metavar="TOKEN",
        help="Resume search from this nextPageToken (Jira /search/jql pagination)",
    )
    parser.add_argument(
        "--probe-jql",
        default=PROBE_JQL_DEFAULT,
        help="JQL used only to pick which Severity field when several exist (default: OAA SCA fixable)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Page size for search (default: 50, max 100 typical)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.15,
        help="Seconds to sleep after each successful label update (default: 0.15)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    dry_run = not args.apply
    if dry_run:
        LOGGER.info("Dry-run mode (no API updates). Use --apply to add labels.")

    base, email, token = load_config()
    session = jira_session(base, email, token)
    severity_id, introduced_id = resolve_custom_field_ids(
        session, base, args.probe_jql
    )

    stats: Dict[str, int] = {}
    errors = 0
    next_token: Optional[str] = args.next_page_token
    issue_count = 0

    while True:
        data = search_jql_page(
            session,
            base,
            args.jql,
            severity_id,
            introduced_id,
            args.max_results,
            next_token,
        )
        issues = data.get("issues") or []
        issue_count += len(issues)

        for issue in issues:
            outcome = process_issue(
                issue,
                severity_id,
                introduced_id,
                session,
                base,
                dry_run,
                args.sleep,
            )
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome == "error_update":
                errors += 1

        next_token = data.get("nextPageToken")
        if data.get("isLast") or not next_token:
            break

    LOGGER.info(
        "Finished. issues_processed=%s counts=%s update_errors=%s dry_run=%s",
        issue_count,
        stats,
        errors,
        dry_run,
    )
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
