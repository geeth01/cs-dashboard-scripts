#!/usr/bin/env python3
"""
Standalone **SLA breach latency** stats aligned with Compliance sheet SLA **calendar math**.

**Breached fixes** counted here **only when** Severity + vulnerability introduced date +
resolution yield a breach (`resolved_date` strictly after introduced + SLA days).
The ``sla-breached`` Jira label is **not** used (label automation lag should not skew counts).

Lead time mean (calendar days):

- ``avg_resolution_age_days_breached`` — resolved_date − introduced_date (breaches only).

Past-SLA lag at resolution (breaches with full SLA math sample only):

- ``avg_days_past_sla_at_resolution`` — mean(resolved_date − SLA due date).
- ``median_days_past_sla_at_resolution`` — median of the same deltas.
- ``p90_days_past_sla_at_resolution`` — 90th percentile (linear interpolation).

Cohort matches ``jira_compliance_daily.py``: ``base_scope`` from ``jira_metrics_queries.json`` plus
SCA/SAST, fixable, not ignored, resolved in ``[--start, --end]``.

Environment: ``JIRA_BASE_URL``, ``JIRA_EMAIL``, ``JIRA_API_TOKEN``; optional ``JIRA_COMPLIANCE_*``,
``JIRA_FIELD_SEVERITY``, ``JIRA_FIELD_VULNERABILITY_INTRODUCED``, ``JIRA_METRICS_JSON``.

Examples::

  python3 jira_breach_latency_averages.py
  python3 jira_breach_latency_averages.py --start 2026-02-01 --end 2026-05-14
  python3 jira_breach_latency_averages.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from jira_compliance_daily import (
    DEFAULT_METRICS_JSON,
    build_jql,
    build_probe_jql,
    compute_breach_latency_portfolio_metrics,
    fetch_all_issues_jql,
    jira_session,
    load_base_scope,
    load_config,
    resolve_project_filter,
)
from jira_sla_breach_label import try_resolve_custom_field_ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Portfolio breach latency (calendar SLA math only; ignores sla-breached label): "
            "means plus median/p90 days past SLA."
        )
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Resolved window start YYYY-MM-DD (inclusive). Default: env JIRA_COMPLIANCE_START or 2026-02-01",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Resolved window end YYYY-MM-DD (inclusive). Default: today in --timezone",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="IANA tz for default --end day (default UTC). Also reads JIRA_COMPLIANCE_TIMEZONE.",
    )
    parser.add_argument(
        "--metrics-json",
        type=Path,
        default=DEFAULT_METRICS_JSON,
        help="Path to jira_metrics_queries.json",
    )
    parser.add_argument(
        "--projects",
        type=str,
        default=None,
        help="Comma-separated Jira project keys (overrides env / file like jira_compliance_daily)",
    )
    parser.add_argument(
        "--projects-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Project keys file if --projects and JIRA_COMPLIANCE_PROJECTS unset",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Jira search page size",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object on stdout (for scripts)",
    )
    args = parser.parse_args()

    base, email, token = load_config()
    session = jira_session(base, email, token)

    tzname = (os.environ.get("JIRA_COMPLIANCE_TIMEZONE") or args.timezone).strip() or args.timezone
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except ImportError:
        print("Error: zoneinfo is required (Python 3.9+).", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: invalid timezone {tzname!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    start_raw = (
        args.start
        or os.environ.get("JIRA_COMPLIANCE_START", "").strip()
        or "2026-02-01"
    )
    start_d = date.fromisoformat(start_raw)

    if args.end:
        end_d = date.fromisoformat(args.end)
    else:
        end_d = datetime.now(tz).date()

    if end_d < start_d:
        print("--end must be on or after --start", file=sys.stderr)
        sys.exit(2)

    resolved_lt = end_d + timedelta(days=1)

    mj_raw = os.environ.get("JIRA_METRICS_JSON", "").strip()
    mj_path = (
        Path(mj_raw).expanduser().resolve()
        if mj_raw
        else Path(args.metrics_json).expanduser().resolve()
    )
    base_scope = load_base_scope(mj_path)
    project_keys = resolve_project_filter(args.projects, args.projects_file)
    probe = build_probe_jql(project_keys, base_scope)

    pair = try_resolve_custom_field_ids(session, base, probe)
    if pair is None:
        print(
            "Failed to resolve Severity / vulnerability_introduced_date; "
            "set JIRA_FIELD_SEVERITY and JIRA_FIELD_VULNERABILITY_INTRODUCED.",
            file=sys.stderr,
        )
        sys.exit(1)
    severity_id, introduced_id = pair

    jql = build_jql(base_scope, start_d, resolved_lt, project_keys)
    fields = ["key", "project", "labels", "resolutiondate", severity_id, introduced_id]

    if not args.json:
        print("Cohort JQL (calendar SLA math; label not used for breach classification):\n")
        print(jql + "\n")

    issues = fetch_all_issues_jql(
        session,
        base,
        jql,
        max_results=args.max_results,
        fields=fields,
    )

    if len(issues) == 0:
        try:
            auth_r = session.get(f"{base}/rest/api/3/myself")
            out_auth: Dict[str, Any] = {
                "diag_rest_myself_http": auth_r.status_code,
            }
            if auth_r.ok:
                me = auth_r.json()
                out_auth["diag_authenticated_email"] = (me or {}).get("emailAddress")
                out_auth["diag_hint"] = (
                    "REST auth works but cohort is empty → usually **no Browse Projects** "
                    "on portfolio keys or JQL not visible to this user. python3 jira_api_probe.py"
                )
            else:
                body = auth_r.text or ""
                out_auth["diag_authenticated_email"] = None
                out_auth["diag_myself_snippet"] = body[:280]
                out_auth["diag_hint"] = (
                    "REST returned non-OK /myself — fix JIRA_EMAIL + JIRA_API_TOKEN "
                    "(regenerate classic API token while logged into the same Navigator account). "
                    "python3 jira_api_probe.py"
                )
        except Exception as exc:
            out_auth = {"diag_hint": f"Could not call /myself: {exc}"}
    else:
        out_auth = {}

    metrics = compute_breach_latency_portfolio_metrics(
        issues,
        breach_by_label=False,
        severity_id=severity_id,
        introduced_id=introduced_id,
        label_fallback=False,
    )

    out: Dict[str, Any] = {
        "resolve_start_inclusive": start_d.isoformat(),
        "resolve_end_inclusive": end_d.isoformat(),
        "timezone": tzname,
        "cohort_issue_count": len(issues),
        "breached_fixes_sla_math_only": metrics.breached_issue_count,
        "breached_with_latency_sample": metrics.breached_with_latency_days_count,
        "avg_resolution_age_days_breached": metrics.avg_resolution_age_days_breached,
        "avg_days_past_sla_at_resolution": metrics.avg_days_past_sla_at_resolution,
        "median_days_past_sla_at_resolution": metrics.median_days_past_sla_at_resolution,
        "p90_days_past_sla_at_resolution": metrics.p90_days_past_sla_at_resolution,
        "jql": jql,
        **out_auth,
    }

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"Issues in cohort: {len(issues):,}", flush=True)
    print(
        f"Breached fixes (SLA calendar math only): {metrics.breached_issue_count:,}",
        flush=True,
    )
    print(
        f"  (those with latency inputs for averages: {metrics.breached_with_latency_days_count:,})",
        flush=True,
    )
    if metrics.avg_resolution_age_days_breached is not None:
        print(
            "Avg calendar days resolved − introduced (breached): "
            f"{metrics.avg_resolution_age_days_breached}",
            flush=True,
        )
        print(
            "Avg calendar days resolved − SLA due date (breached): "
            f"{metrics.avg_days_past_sla_at_resolution}",
            flush=True,
        )
        if metrics.median_days_past_sla_at_resolution is not None:
            print(
                "Median calendar days resolved − SLA due date (breached): "
                f"{metrics.median_days_past_sla_at_resolution}",
                flush=True,
            )
        if metrics.p90_days_past_sla_at_resolution is not None:
            print(
                "90th pct calendar days resolved − SLA due date (breached): "
                f"{metrics.p90_days_past_sla_at_resolution}",
                flush=True,
            )
    elif metrics.breached_issue_count == 0:
        print("No math-based breaches in window; averages N/A.", flush=True)
        if len(issues) == 0 and out.get("diag_hint"):
            print(f"\n{out['diag_hint']}", file=sys.stderr, flush=True)
    else:
        print(
            "Breaches present but averages N/A — resolution/intro/sev parse gap (unexpected); "
            "check custom fields.",
            flush=True,
        )


if __name__ == "__main__":
    main()
