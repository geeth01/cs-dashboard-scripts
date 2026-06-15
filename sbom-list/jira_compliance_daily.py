#!/usr/bin/env python3
"""
Daily SLA compliance per Jira project: % of fixed vulnerability tickets that were
fixed within SLA (no ``sla-breached`` label).

**Daily:** Compliance = 1 - (Breached / Fixed) per day.

**Cumulative** (per project, ordered by date): ``Cum_Fixed`` / ``Cum_Breached`` are
running totals from ``--start``; ``Cumulative_Compliance_pct`` =
``100 * (1 - Cum_Breached / Cum_Fixed)`` (same as ``Cum_Within_SLA / Cum_Fixed``).

Uses ``POST /rest/api/3/search/jql`` (paginated). ``POST /rest/api/3/search`` returns **410 Gone**
on current Cloud sites and is not used. If search returns no rows when SLA custom fields are
requested, the client falls back to key-only search plus ``GET /rest/api/3/issue/{key}`` hydration.

**Breached** (default): among issues **resolved** that day, count how many were **late**
per SLA — same rule as ``jira_sla_breach_label.py`` (resolution **date** strictly after
``introduced_date + N days`` by severity). This **aligns** with **Open_Past_SLA_Count**
(which uses the same introduced/severity windows in JQL). Use ``--breached-by-label``
to count only the ``sla-breached`` Jira label instead (often **0** if labels were never
applied). **Fixed** is the number of issues resolved that day.

By default the CSV includes **every calendar day** from ``--start`` through the end
date (see below) for each project — **no gaps** when ``Fixed`` is 0. Use
``--omit-zero-days`` for the old behavior (only days with at least one fix).

**End date:** if ``--end`` is omitted, the range runs through **today** in
``--timezone`` (local calendar date).

**Open_Past_SLA_Count** (unless ``--no-open-past-sla``): uses **historical** JQL
``status WAS NOT IN (...) ON (Date)`` plus severity/introduced cutoffs — a **snapshot**
of issues that were **not** in terminal statuses **on that calendar day** and were
already past SLA by that day. This is **not** the same as **Breached** (below).

**Cum_Breached** sums daily **Breached** (calculated or label-based per flags above).

Optional **``--kpi-output``** writes a second CSV with clearer headers (e.g.
``Fixed-Total``, ``Fixed-Breached``, ``Open-Breached`` for backlog exposure).

**Projects:** ``--projects`` (comma-separated) overrides env ``JIRA_COMPLIANCE_PROJECTS``,
which overrides ``--projects-file``; if none are set, all projects from
``jira_metrics_queries.json`` ``base_scope`` are included.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, NamedTuple, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

from jira_sla_breach_label import (
    parse_introduced_date,
    parse_resolution_date,
    parse_severity_value,
    resolve_custom_field_ids,
    sla_breached,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

LOGGER = logging.getLogger("jira_compliance_daily")

SLA_LABEL = "sla-breached"

# Same calendar-day SLA windows as jira_sla_breach_label.py / jira_metrics_queries.json
SEVERITY_SLA_DAYS = {
    "Sev-0": 14,
    "Sev-1": 30,
    "Sev-2": 90,
    "Sev-3": 180,
}

DEFAULT_METRICS_JSON = Path(__file__).resolve().parent / "jira_metrics_queries.json"


def load_config() -> Tuple[str, str, str]:
    """Load `.env`: repository directory first (works regardless of cwd), then process env."""
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


def load_base_scope(metrics_path: Path) -> str:
    with open(metrics_path, encoding="utf-8") as f:
        data = json.load(f)
    scope = (data.get("base_scope") or "").strip()
    if not scope:
        LOGGER.error("base_scope missing in %s", metrics_path)
        sys.exit(1)
    return scope


def build_jql(
    base_scope: str,
    resolved_gte: date,
    resolved_lt: date,
    project_keys: Optional[Set[str]],
) -> str:
    """resolved_gte inclusive, resolved_lt exclusive (UTC day boundaries in JQL string)."""
    parts = [
        "(" + base_scope + ")",
        '(labels = "vulnerability/sca" OR labels = "vulnerability/sast")',
        'labels = "vulnerability/fixable"',
        'labels != "vulnerability/ignored"',
        "resolution IS NOT EMPTY",
    ]
    if project_keys:
        plist = ", ".join(sorted(project_keys))
        parts.append(f"project in ({plist})")
    gte = resolved_gte.strftime("%Y-%m-%d")
    lt = resolved_lt.strftime("%Y-%m-%d")
    parts.append(f'resolved >= "{gte}"')
    parts.append(f'resolved < "{lt}"')
    return " AND ".join(parts)


def _normalize_next_page_token(data: Dict[str, Any]) -> Optional[str]:
    """Treat blank tokens as absent so pagination does not stop early."""
    t = data.get("nextPageToken")
    if t is None:
        return None
    if isinstance(t, str) and not t.strip():
        return None
    return str(t)


def approximate_jql_count(
    session: requests.Session,
    base: str,
    jql: str,
) -> Optional[int]:
    """Best-effort issue count for diagnostics (same visibility rules as search)."""
    try:
        r = session.post(
            f"{base}/rest/api/3/search/approximate-count",
            json={"jql": jql},
        )
        if not r.ok:
            LOGGER.debug(
                "approximate-count HTTP %s: %s",
                r.status_code,
                r.text[:400],
            )
            return None
        raw = r.json().get("count")
        return int(raw) if raw is not None else None
    except (requests.RequestException, ValueError, TypeError) as exc:
        LOGGER.debug("approximate-count failed: %s", exc)
        return None


def search_jql_page(
    session: requests.Session,
    base: str,
    jql: str,
    max_results: int,
    next_page_token: Optional[str],
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": fields or ["key", "project", "labels", "resolutiondate"],
    }
    if next_page_token:
        body["nextPageToken"] = next_page_token
    r = session.post(f"{base}/rest/api/3/search/jql", json=body)
    r.raise_for_status()
    return r.json()


def _paginate_post_search_jql(
    session: requests.Session,
    base: str,
    jql: str,
    *,
    max_results: int,
    fields: List[str],
) -> List[Dict[str, Any]]:
    """Paginate ``POST /rest/api/3/search/jql``."""
    issues: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        data = search_jql_page(session, base, jql, max_results, next_token, fields=fields)
        issues.extend(data.get("issues") or [])
        next_token = _normalize_next_page_token(data)
        if data.get("isLast") or not next_token:
            break
    return issues


def _paginate_post_search_jql_issue_refs(
    session: requests.Session,
    base: str,
    jql: str,
    *,
    max_results: int,
) -> List[str]:
    """
    Paginate POST search **without** a ``fields`` array — returns minimal refs (``key`` or ``id``).

    Some tenants return rows here when specifying ``fields`` yields an empty ``issues`` list.
    """
    refs: List[str] = []
    next_token: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"jql": jql, "maxResults": max_results}
        if next_token:
            body["nextPageToken"] = next_token
        r = session.post(f"{base}/rest/api/3/search/jql", json=body)
        r.raise_for_status()
        data = r.json()
        for it in data.get("issues") or []:
            ref = it.get("key") or it.get("id")
            if ref:
                refs.append(str(ref))
        next_token = _normalize_next_page_token(data)
        if data.get("isLast") or not next_token:
            break
    return list(dict.fromkeys(refs))


def _paginate_get_search_jql(
    session: requests.Session,
    base: str,
    jql: str,
    *,
    max_results: int,
    fields: List[str],
) -> List[Dict[str, Any]]:
    """Paginate ``GET /rest/api/3/search/jql`` (fallback when POST returns no usable rows)."""
    issues: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        params: List[Tuple[str, str]] = [
            ("jql", jql),
            ("maxResults", str(max_results)),
        ]
        if next_token:
            params.append(("nextPageToken", next_token))
        for f in fields:
            params.append(("fields", f))
        r = session.get(f"{base}/rest/api/3/search/jql", params=params)
        r.raise_for_status()
        data = r.json()
        issues.extend(data.get("issues") or [])
        next_token = _normalize_next_page_token(data)
        if data.get("isLast") or not next_token:
            break
    return issues


def _hydrate_issues_by_keys(
    session: requests.Session,
    base: str,
    keys_or_ids: List[str],
    fields: List[str],
) -> List[Dict[str, Any]]:
    """Full issue payloads via ``GET /rest/api/3/issue/{issueIdOrKey}`` (parallel, session per thread)."""
    if not keys_or_ids:
        return []
    fields_q = ",".join(fields)
    try:
        workers = int(os.environ.get("JIRA_ISSUE_FETCH_WORKERS", "8").strip() or "8")
    except ValueError:
        workers = 8
    workers = max(1, min(workers, 16))

    auth = getattr(session, "auth", None)
    headers = dict(session.headers)
    proxies = getattr(session, "proxies", None) or {}

    def fetch_one(ref: str) -> Dict[str, Any]:
        s = requests.Session()
        if auth is not None:
            s.auth = auth  # type: ignore[method-assign]
        s.headers.update(headers)
        if proxies:
            s.proxies.update(proxies)
        r = s.get(f"{base}/rest/api/3/issue/{ref}", params={"fields": fields_q})
        r.raise_for_status()
        return r.json()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fetch_one, keys_or_ids))


def fetch_all_issues_jql(
    session: requests.Session,
    base: str,
    jql: str,
    *,
    max_results: int,
    fields: List[str],
) -> List[Dict[str, Any]]:
    """
    Paginate enhanced search.

    ``POST /rest/api/3/search`` returns **410 Gone** on Jira Cloud — do not use it.

    If the first search returns **no issues** while requesting SLA custom fields (known tenant
    behaviour), retry with ``fields=["key"]`` only and hydrate each issue via ``GET`` .
    """
    issues = _paginate_post_search_jql(
        session, base, jql, max_results=max_results, fields=fields
    )
    if issues:
        return issues

    LOGGER.warning(
        "POST /rest/api/3/search/jql returned no rows with requested fields; "
        "retrying with fields=['key'] only, then GET /rest/api/3/issue/{{key}}."
    )
    print(
        "Note: Jira search returned 0 rows with SLA fields; fetching keys only, "
        "then hydrating via GET /rest/api/3/issue/{key}.",
        file=sys.stderr,
        flush=True,
    )

    keyed = _paginate_post_search_jql(
        session, base, jql, max_results=max_results, fields=["key"]
    )
    refs = list(
        dict.fromkeys(str(it["key"]) for it in keyed if it.get("key"))
    )

    if not refs:
        LOGGER.warning(
            "POST key-only search empty; trying POST without ``fields`` (minimal refs)."
        )
        print(
            "Note: Trying Jira search without a ``fields`` payload (minimal issue refs).",
            file=sys.stderr,
            flush=True,
        )
        refs = _paginate_post_search_jql_issue_refs(
            session, base, jql, max_results=max_results
        )

    if not refs:
        max_get_jql = int(os.environ.get("JIRA_SEARCH_GET_MAX_JQL_CHARS", "3500").strip() or "3500")
        if len(jql) <= max_get_jql:
            LOGGER.warning("Minimal-ref POST empty; trying GET /rest/api/3/search/jql.")
            try:
                keyed = _paginate_get_search_jql(
                    session, base, jql, max_results=max_results, fields=["key"]
                )
                refs = list(
                    dict.fromkeys(str(it["key"]) for it in keyed if it.get("key"))
                )
            except requests.RequestException as exc:
                LOGGER.warning("GET /rest/api/3/search/jql failed: %s", exc)
                refs = []
        else:
            print(
                f"Note: Skipping GET /search/jql (JQL length {len(jql)} > {max_get_jql}); "
                "long queries exceed safe URL limits.",
                file=sys.stderr,
                flush=True,
            )

    if not refs:
        approx = approximate_jql_count(session, base, jql)
        if approx is not None:
            print(f"Note: Jira approximate-count for this JQL: {approx:,}", flush=True)
            if approx > 0:
                print(
                    "Warning: Search APIs returned no rows but approximate-count > 0 — "
                    "the API token user may lack Browse permission on these projects, "
                    "or Jira search indexing is inconsistent. Compare in Navigator logged "
                    "in as that same Atlassian account.",
                    file=sys.stderr,
                    flush=True,
                )
            elif approx == 0:
                print(
                    "Note: Approximate-count is also 0 — this JQL likely matches nothing "
                    "for issues visible to this token.",
                    flush=True,
                )
        return []

    LOGGER.info("Hydrating %s issues from Jira", len(refs))
    print(
        f"Note: Hydrating {len(refs):,} issues via GET /rest/api/3/issue/{{issueIdOrKey}} …",
        flush=True,
    )
    return _hydrate_issues_by_keys(session, base, refs, fields)


def count_jql_issues(
    session: requests.Session,
    base: str,
    jql: str,
    max_results: int,
) -> int:
    """Total issues matching JQL (paginated)."""
    total = 0
    next_token: Optional[str] = None
    while True:
        data = search_jql_page(
            session, base, jql, max_results, next_token, fields=["key"]
        )
        total += len(data.get("issues") or [])
        next_token = _normalize_next_page_token(data)
        if data.get("isLast") or not next_token:
            break
    return total


def build_open_past_sla_jql(
    base_scope: str,
    project_key: str,
    snapshot_date: date,
    done_statuses: List[str],
) -> str:
    """
    Point-in-time open backlog past SLA on ``snapshot_date`` using **historical** JQL.

    Uses ``status WAS NOT IN (...) ON (date)`` so counts match that **day's** workflow
    state — **not** ``status not in (...)`` (current), which wrongly shows 0 after
    issues close later.

    Breach if introduced_date + N < snapshot_date  <=>
    introduced_date < snapshot_date - N days (strict).
    """
    sev_parts: List[str] = []
    for sev, n in SEVERITY_SLA_DAYS.items():
        cutoff = (snapshot_date - timedelta(days=n)).isoformat()
        sev_parts.append(
            f'("Severity[Dropdown]" = {sev} AND '
            f'"vulnerability_introduced_date[Date]" < "{cutoff}")'
        )
    inner = ", ".join(done_statuses)
    ds = snapshot_date.strftime("%Y-%m-%d")
    status_snapshot = f"status WAS NOT IN ({inner}) ON ({ds})"
    parts = [
        "(" + base_scope + ")",
        '(labels = "vulnerability/sca" OR labels = "vulnerability/sast")',
        'labels = "vulnerability/fixable"',
        'labels != "vulnerability/ignored"',
        f"project = {project_key}",
        status_snapshot,
        "(" + " OR ".join(sev_parts) + ")",
    ]
    return " AND ".join(parts)


def parse_resolution_utc(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def project_key(issue: Dict[str, Any]) -> Optional[str]:
    proj = (issue.get("fields") or {}).get("project")
    if not isinstance(proj, dict):
        return None
    k = proj.get("key")
    return str(k) if k else None


def aggregate_issues(
    issues: List[Dict[str, Any]],
    tz: Any,
    severity_id: Optional[str],
    introduced_id: Optional[str],
    breach_by_label: bool,
) -> DefaultDict[Tuple[str, date], Dict[str, int]]:
    """
    Count fixed and breached per (project_key, local_calendar_date).

    If ``breach_by_label``, Breached uses the ``sla-breached`` label only.
    Otherwise Breached uses ``sla_breached()`` date math (same as label automation);
    falls back to label if severity/introduced/resolution is missing.
    """
    counts: DefaultDict[Tuple[str, date], Dict[str, int]] = defaultdict(
        lambda: {"fixed": 0, "breached": 0}
    )
    for issue in issues:
        pk = project_key(issue)
        if not pk:
            continue
        fields = issue.get("fields") or {}
        labels = fields.get("labels") or []
        res = parse_resolution_utc(fields.get("resolutiondate"))
        if res is None:
            continue
        if tz is not None:
            local = res.astimezone(tz)
        else:
            local = res.astimezone(timezone.utc)
        d = local.date()
        cell = counts[(pk, d)]
        cell["fixed"] += 1

        if breach_by_label:
            if SLA_LABEL in labels:
                cell["breached"] += 1
            continue

        assert severity_id and introduced_id
        sev = parse_severity_value(fields.get(severity_id))
        intro = parse_introduced_date(fields.get(introduced_id))
        res_date = parse_resolution_date(fields.get("resolutiondate"))
        breached_inc = False
        if (
            sev
            and intro
            and res_date
            and sev in SEVERITY_SLA_DAYS
        ):
            br, _ = sla_breached(intro, sev, res_date)
            breached_inc = br
        elif SLA_LABEL in labels:
            breached_inc = True
        if breached_inc:
            cell["breached"] += 1
    return counts


def _linear_percentile(values: List[int], p_pct: float) -> float:
    """Linear interpolation percentile over ``values``; ``p_pct`` in [0, 100]."""
    if not values:
        raise ValueError("percentile requires at least one value")
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    rank = (n - 1) * (p_pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    return s[lo] + (rank - lo) * (s[hi] - s[lo])


class BreachLatencyPortfolioMetrics(NamedTuple):
    """
    Portfolio-wide breached-fix latency from raw Jira issues.

    When ``label_fallback`` is enabled (same as ``aggregate_issues``), breaches can be flagged
    via ``sla-breached`` only if SLA fields are missing. ``avg_*`` always include only rows with
    a full SLA math path (introduced + severity + resolution + breach).

    Set ``label_fallback=False`` so ``breached_issue_count`` counts **only** math-based breaches —
    aligns standalone latency reports with “no label rule” summaries.
    """

    breached_issue_count: int
    breached_with_latency_days_count: int
    avg_resolution_age_days_breached: Optional[float]
    avg_days_past_sla_at_resolution: Optional[float]
    median_days_past_sla_at_resolution: Optional[float]
    p90_days_past_sla_at_resolution: Optional[float]


def compute_breach_latency_portfolio_metrics(
    issues: List[Dict[str, Any]],
    *,
    breach_by_label: bool,
    severity_id: Optional[str],
    introduced_id: Optional[str],
    label_fallback: bool = True,
) -> BreachLatencyPortfolioMetrics:
    """
    Mean calendar days for breached resolutions:

    - **Resolution age (breached only):** ``resolved_date - introduced_date`` (FY25-style lead time).
    - **Past SLA at resolution:** ``resolved_date - due_date`` where ``due = introduced + SLA_days``.

    Distribution of that **past-SLA** lag (breached issues with full math path only): **mean** (``avg_*``),
    **median**, and **90th percentile** (tail). All use **calendar days**.

    ``avg_*`` / median / p90 use only rows with full SLA math. If ``label_fallback`` is ``True``, issues
    with only ``sla-breached`` (no SLA fields) still increment ``breached_issue_count``.
    Use ``label_fallback=False`` to count breached fixes **only by calendar SLA math**.
    """
    breached_issue_count = 0
    sum_lead = 0
    sum_past = 0
    breached_with_latency_days_count = 0
    past_sla_day_samples: List[int] = []

    for issue in issues:
        pk = project_key(issue)
        if not pk:
            continue
        fields = issue.get("fields") or {}
        labels = fields.get("labels") or []
        res = parse_resolution_utc(fields.get("resolutiondate"))
        if res is None:
            continue

        breached_inc = False
        lead_days: Optional[int] = None
        past_days: Optional[int] = None

        if breach_by_label:
            if SLA_LABEL in labels:
                breached_inc = True
        else:
            assert severity_id and introduced_id
            sev = parse_severity_value(fields.get(severity_id))
            intro = parse_introduced_date(fields.get(introduced_id))
            res_date = parse_resolution_date(fields.get("resolutiondate"))
            if sev and intro and res_date and sev in SEVERITY_SLA_DAYS:
                br, n = sla_breached(intro, sev, res_date)
                breached_inc = br
                if br and n is not None:
                    due = intro + timedelta(days=n)
                    lead_days = (res_date - intro).days
                    past_days = (res_date - due).days
            elif label_fallback and SLA_LABEL in labels:
                breached_inc = True

        if not breached_inc:
            continue

        breached_issue_count += 1
        if lead_days is not None and past_days is not None:
            sum_lead += lead_days
            sum_past += past_days
            breached_with_latency_days_count += 1
            past_sla_day_samples.append(past_days)

    avg_lead: Optional[float]
    avg_past: Optional[float]
    med_past: Optional[float] = None
    p90_past: Optional[float] = None
    if breached_with_latency_days_count > 0:
        avg_lead = round(sum_lead / breached_with_latency_days_count, 1)
        avg_past = round(sum_past / breached_with_latency_days_count, 1)
        med_past = round(float(statistics.median(past_sla_day_samples)), 1)
        p90_past = round(_linear_percentile(past_sla_day_samples, 90.0), 1)
    else:
        avg_lead = None
        avg_past = None

    return BreachLatencyPortfolioMetrics(
        breached_issue_count=breached_issue_count,
        breached_with_latency_days_count=breached_with_latency_days_count,
        avg_resolution_age_days_breached=avg_lead,
        avg_days_past_sla_at_resolution=avg_past,
        median_days_past_sla_at_resolution=med_past,
        p90_days_past_sla_at_resolution=p90_past,
    )


def compliance_pct(fixed: int, breached: int) -> Optional[float]:
    if fixed <= 0:
        return None
    return round(100.0 * (1.0 - (breached / fixed)), 2)


def attach_open_past_sla_counts(
    session: requests.Session,
    base: str,
    base_scope: str,
    rows: List[Dict[str, Any]],
    max_results: int,
    sleep_s: float,
    done_statuses: List[str],
) -> None:
    """Mutate rows in place: set Open_Past_SLA_Count per (Project, Date)."""
    cache: Dict[Tuple[str, str], int] = {}
    for row in rows:
        pk = str(row["Project"])
        d_str = str(row["Date"])
        key = (pk, d_str)
        if key in cache:
            row["Open_Past_SLA_Count"] = cache[key]
            continue
        snap = date.fromisoformat(d_str)
        ojql = build_open_past_sla_jql(base_scope, pk, snap, done_statuses)
        LOGGER.debug("Open past SLA JQL %s %s: %s", pk, d_str, ojql)
        n = count_jql_issues(session, base, ojql, max_results)
        cache[key] = n
        row["Open_Past_SLA_Count"] = n
        if sleep_s > 0:
            time.sleep(sleep_s)


def apply_cumulative_metrics(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort by Project, Date; add Cum_Fixed, Cum_Breached, Cum_Within_SLA,
    Cumulative_Compliance_pct (running totals reset per project).
    """
    sorted_rows = sorted(rows, key=lambda r: (r["Project"], r["Date"]))
    out: List[Dict[str, Any]] = []
    cum_f = 0
    cum_b = 0
    cur_proj: Optional[str] = None
    for r in sorted_rows:
        pk = str(r["Project"])
        if pk != cur_proj:
            cum_f = 0
            cum_b = 0
            cur_proj = pk
        fixed = int(r["Fixed"])
        breached = int(r["Breached"])
        cum_f += fixed
        cum_b += breached
        cum_w = cum_f - cum_b
        cc = compliance_pct(cum_f, cum_b)
        row = dict(r)
        row["Cum_Fixed"] = cum_f
        row["Cum_Breached"] = cum_b
        row["Cum_Within_SLA"] = cum_w
        row["Cumulative_Compliance_pct"] = (
            f"{cc}%" if cum_f > 0 and cc is not None else ""
        )
        # preserve Open_Past_SLA_Count if present
        out.append(row)
    return out


def write_issues_csv(
    path: Path,
    issues: List[Dict[str, Any]],
    tz: Any,
) -> None:
    fieldnames = [
        "Project",
        "Issue",
        "Resolved_UTC",
        "Resolved_Local_Date",
        "Labels",
        "SLA_Breached",
    ]
    rows: List[Dict[str, str]] = []
    for issue in issues:
        pk = project_key(issue) or ""
        key = issue.get("key") or ""
        fields = issue.get("fields") or {}
        labels = fields.get("labels") or []
        res = parse_resolution_utc(fields.get("resolutiondate"))
        if res is None:
            continue
        utc_s = res.strftime("%Y-%m-%dT%H:%M:%SZ")
        local = res.astimezone(tz)
        local_d = local.date().isoformat()
        breached = "yes" if SLA_LABEL in labels else "no"
        rows.append(
            {
                "Project": pk,
                "Issue": key,
                "Resolved_UTC": utc_s,
                "Resolved_Local_Date": local_d,
                "Labels": " ".join(sorted(labels)),
                "SLA_Breached": breached,
            }
        )
    rows.sort(key=lambda r: (r["Project"], r["Resolved_Local_Date"], r["Issue"]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


CSV_DISPLAY_COLUMNS = [
    "Project",
    "Date",
    "Fixed-Total",
    "Fixed-Breached",
    "Fixed-Within_SLA",
    "Daily-Compliance",
    "Open-Breached",
    "Cumulative-Fixed-Total",
    "Cumulative-Fixed-Breached",
    "Cumulative-Fixed-Within_SLA",
    "Cumulative-Compliance",
]


def row_to_display_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map internal row keys to CSV column headers (throughput vs backlog labels)."""
    return {
        "Project": row["Project"],
        "Date": row["Date"],
        "Fixed-Total": row["Fixed"],
        "Fixed-Breached": row["Breached"],
        "Fixed-Within_SLA": row["Within_SLA"],
        "Daily-Compliance": row["Daily_Compliance_pct"],
        "Open-Breached": row.get("Open_Past_SLA_Count", ""),
        "Cumulative-Fixed-Total": row["Cum_Fixed"],
        "Cumulative-Fixed-Breached": row["Cum_Breached"],
        "Cumulative-Fixed-Within_SLA": row["Cum_Within_SLA"],
        "Cumulative-Compliance": row["Cumulative_Compliance_pct"],
    }


def write_csv(
    path: Path,
    rows: List[Dict[str, Any]],
) -> None:
    """
    Write main report: same column names as ``--kpi-output`` (Fixed-Total,
    Open-Breached, Daily-Compliance, etc.).
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_DISPLAY_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow(row_to_display_dict(row))


def write_kpi_split_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    """Optional second file: identical format to ``-o`` / ``write_csv`` (kept for extra path)."""
    write_csv(path, rows)


def parse_project_list(s: Optional[str]) -> Optional[Set[str]]:
    if not s or not s.strip():
        return None
    return {p.strip().upper() for p in s.split(",") if p.strip()}


def load_projects_from_file(path: Path) -> Set[str]:
    """One project key per line, or comma-separated; ``#`` starts a comment."""
    text = path.read_text(encoding="utf-8")
    keys: Set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.split(","):
            p = part.strip().upper()
            if p:
                keys.add(p)
    if not keys:
        LOGGER.error("No project keys found in %s", path)
        sys.exit(1)
    return keys


def resolve_project_filter(
    cli_projects: Optional[str],
    projects_file: Optional[Path],
) -> Optional[Set[str]]:
    """
    Which Jira projects to report on.

    Precedence: ``--projects`` > ``JIRA_COMPLIANCE_PROJECTS`` (env / .env) >
    ``--projects-file`` > None (all projects in ``base_scope``).
    """
    raw = (cli_projects or "").strip()
    if raw:
        return parse_project_list(raw)
    env = (os.environ.get("JIRA_COMPLIANCE_PROJECTS") or "").strip()
    if env:
        pf = parse_project_list(env)
        if pf:
            LOGGER.info(
                "Using projects from JIRA_COMPLIANCE_PROJECTS: %s",
                ", ".join(sorted(pf)),
            )
            return pf
    if projects_file is not None:
        keys = load_projects_from_file(projects_file)
        LOGGER.info(
            "Using projects from --projects-file %s: %s",
            projects_file,
            ", ".join(sorted(keys)),
        )
        return keys
    return None


def parse_projects_from_base_scope(scope: str) -> List[str]:
    """Extract project keys from ``project in (A, B, ...)``."""
    m = re.search(r"project\s+in\s*\(([^)]+)\)", scope, re.I)
    if not m:
        return []
    raw = m.group(1)
    keys: List[str] = []
    for part in raw.split(","):
        p = part.strip().strip("'\"")
        if p:
            keys.append(p.upper())
    return keys


def iter_dates_inclusive(start: date, end: date) -> List[date]:
    out: List[date] = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def build_probe_jql(
    project_filter: Optional[Set[str]], base_scope: str
) -> str:
    """JQL sample for resolving which custom field holds Severity (see label script)."""
    if project_filter and len(project_filter) == 1:
        p = sorted(project_filter)[0]
        return (
            f'project = {p} AND labels = "vulnerability/sca" '
            'AND labels = "vulnerability/fixable" ORDER BY updated DESC'
        )
    plist = parse_projects_from_base_scope(base_scope)
    if plist:
        p = plist[0]
        return (
            f'project = {p} AND labels = "vulnerability/sca" '
            'AND labels = "vulnerability/fixable" ORDER BY updated DESC'
        )
    return (
        'labels = "vulnerability/sca" AND labels = "vulnerability/fixable" '
        "ORDER BY updated DESC"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export daily SLA compliance per project (fixed vs sla-breached) to CSV."
    )
    parser.add_argument(
        "--start",
        type=str,
        required=True,
        help="Start date (inclusive), ISO YYYY-MM-DD",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="End date (inclusive), ISO. If omitted, uses today in --timezone",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="IANA timezone for bucketing resolution to calendar days (default: UTC)",
    )
    parser.add_argument(
        "--metrics-json",
        type=Path,
        default=DEFAULT_METRICS_JSON,
        help="Path to jira_metrics_queries.json (for base_scope project list)",
    )
    parser.add_argument(
        "--projects",
        type=str,
        default=None,
        metavar="KEYS",
        help="Comma-separated Jira project keys (highest precedence). Example: OAA,AH",
    )
    parser.add_argument(
        "--projects-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="File with project keys (one per line or comma-separated; # comments ok). "
        "Used if --projects is omitted and JIRA_COMPLIANCE_PROJECTS env is unset.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("jira_compliance_daily.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Page size for Jira search (default: 100)",
    )
    parser.add_argument(
        "--omit-zero-days",
        action="store_true",
        help="Only emit days with Fixed>0 (skip idle calendar days). Default: include all days",
    )
    parser.add_argument(
        "--breached-by-label",
        action="store_true",
        help="Count Breached using only the sla-breached label. Default: SLA date math "
        "(introduced + severity + resolution), aligned with Open_Past_SLA_Count",
    )
    parser.add_argument(
        "--issues-output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional CSV: one row per resolved issue (audit detail)",
    )
    parser.add_argument(
        "--kpi-output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional extra copy of the report (same columns as -o: Fixed-Total, "
        "Open-Breached, Daily-Compliance, …)",
    )
    parser.add_argument(
        "--no-open-past-sla",
        action="store_true",
        help="Do not query Jira for open backlog past-SLA count (extra API calls per row)",
    )
    parser.add_argument(
        "--open-past-sla-sleep",
        type=float,
        default=0.05,
        help="Seconds to sleep between open-past-SLA count queries (default: 0.05)",
    )
    parser.add_argument(
        "--open-past-sla-done-statuses",
        default="Done, Archived, Rejected",
        help="Terminal status names for historical WAS NOT IN ... ON (Date) (comma-separated)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if ZoneInfo is None:
        LOGGER.error("Python 3.9+ required (zoneinfo).")
        sys.exit(1)

    try:
        start_d = date.fromisoformat(args.start.strip())
    except ValueError as e:
        LOGGER.error("Invalid --start: %s", e)
        sys.exit(1)

    try:
        tz = ZoneInfo(args.timezone)
    except Exception as e:
        LOGGER.error("Invalid --timezone %r: %s", args.timezone, e)
        sys.exit(1)

    if args.end:
        try:
            end_d = date.fromisoformat(args.end.strip())
        except ValueError as e:
            LOGGER.error("Invalid --end: %s", e)
            sys.exit(1)
    else:
        end_d = datetime.now(tz).date()
        LOGGER.info(
            "No --end: using today in %s: %s", args.timezone, end_d.isoformat()
        )

    if end_d < start_d:
        LOGGER.error("--end must be on or after --start")
        sys.exit(1)

    resolved_lt = end_d + timedelta(days=1)
    base_scope = load_base_scope(args.metrics_json)
    project_filter = resolve_project_filter(args.projects, args.projects_file)
    if project_filter is None:
        LOGGER.info(
            "Projects: all keys from base_scope in %s (use --projects, "
            "JIRA_COMPLIANCE_PROJECTS, or --projects-file to limit)",
            args.metrics_json,
        )

    jql = build_jql(base_scope, start_d, resolved_lt, project_filter)
    LOGGER.debug("JQL: %s", jql)

    base, email, token = load_config()
    session = jira_session(base, email, token)

    severity_id: Optional[str] = None
    introduced_id: Optional[str] = None
    issue_fields: List[str] = ["key", "project", "labels", "resolutiondate"]
    if not args.breached_by_label:
        probe_jql = build_probe_jql(project_filter, base_scope)
        severity_id, introduced_id = resolve_custom_field_ids(session, base, probe_jql)
        issue_fields = issue_fields + [severity_id, introduced_id]
        LOGGER.info(
            "Breached column: SLA date math (same as jira_sla_breach_label.py); "
            "fields %s, %s",
            severity_id,
            introduced_id,
        )
    else:
        LOGGER.info("Breached column: sla-breached label only (--breached-by-label)")

    all_issues = fetch_all_issues_jql(
        session,
        base,
        jql,
        max_results=args.max_results,
        fields=issue_fields,
    )

    LOGGER.info("Fetched %s issues", len(all_issues))

    counts = aggregate_issues(
        all_issues,
        tz,
        severity_id,
        introduced_id,
        args.breached_by_label,
    )

    if project_filter:
        plist = sorted(project_filter)
    else:
        plist = parse_projects_from_base_scope(base_scope)
        if not plist:
            plist = sorted({k for (k, _) in counts.keys()})

    if args.omit_zero_days:
        keys_to_emit = sorted(counts.keys())
    else:
        keys_to_emit = [
            (pk, d) for pk in plist for d in iter_dates_inclusive(start_d, end_d)
        ]

    out_rows: List[Dict[str, Any]] = []
    for pk, d in keys_to_emit:
        c = counts.get((pk, d), {"fixed": 0, "breached": 0})
        fixed = c["fixed"]
        breached = c["breached"]
        within = fixed - breached
        daily_pct = compliance_pct(fixed, breached)
        daily_str = f"{daily_pct}%" if fixed > 0 and daily_pct is not None else ""
        out_rows.append(
            {
                "Project": pk,
                "Date": d.isoformat(),
                "Fixed": fixed,
                "Breached": breached,
                "Within_SLA": within,
                "Daily_Compliance_pct": daily_str,
            }
        )

    if not args.no_open_past_sla:
        done_for_open = [
            x.strip() for x in args.open_past_sla_done_statuses.split(",") if x.strip()
        ]
        if not done_for_open:
            LOGGER.error("--open-past-sla-done-statuses cannot be empty")
            sys.exit(1)
        attach_open_past_sla_counts(
            session,
            base,
            base_scope,
            out_rows,
            args.max_results,
            args.open_past_sla_sleep,
            done_for_open,
        )
    else:
        for row in out_rows:
            row["Open_Past_SLA_Count"] = ""

    out_rows = apply_cumulative_metrics(out_rows)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, out_rows)
    LOGGER.info("Wrote %s rows to %s", len(out_rows), args.output)

    if args.kpi_output:
        args.kpi_output.parent.mkdir(parents=True, exist_ok=True)
        write_kpi_split_csv(args.kpi_output, out_rows)
        LOGGER.info("Wrote KPI-split CSV (%s rows) to %s", len(out_rows), args.kpi_output)

    if args.issues_output:
        args.issues_output.parent.mkdir(parents=True, exist_ok=True)
        write_issues_csv(args.issues_output, all_issues, tz)
        LOGGER.info("Wrote issue detail rows to %s", args.issues_output)


if __name__ == "__main__":
    main()
