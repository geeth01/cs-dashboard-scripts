#!/usr/bin/env python3
"""
Reconcile "breached SLA" counts between:

1. **Compliance sheet semantics** (`JiraComplianceDaily.gs` / `jira_compliance_daily.py` default):
   among resolved vulnerability fixes (SCA/SAST + fixable + not ignored), *Breached* uses
   **SLA calendar math** (introduced date + Sev-based window vs resolution **date**). The
   `sla-breached` label is used **only when** severity / introduced / resolution cannot run
   that math.

2. **Navigator shortcut** (`labels = "sla-breached" AND project IN (...) AND status = Done`):
   counts **label presence**, not necessarily the sheet's cohort or date window.

Typical symptom: sheet **Cumulative-Fixed-Breached** >> JQL count on labels alone —
many issues breach by **math** but never received the automation label.

Uses the same `.env` as other Jira scripts (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
optional `JIRA_FIELD_*`, `JIRA_COMPLIANCE_START`).

Examples:

  python3 sla_breach_reconcile.py
  python3 sla_breach_reconcile.py --start 2026-02-01 --end 2026-05-14
  python3 sla_breach_reconcile.py --compare-jql 'labels = "sla-breached" AND project = AH AND status = Done'
  python3 sla_breach_reconcile.py --csv-out breach_buckets.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

from jira_compliance_daily import (
    DEFAULT_METRICS_JSON,
    approximate_jql_count,
    build_jql,
    build_probe_jql,
    count_jql_issues,
    fetch_all_issues_jql,
    jira_session,
    load_base_scope,
    load_config,
    parse_resolution_utc,
    project_key,
    resolve_project_filter,
)
from jira_sla_breach_label import (
    SLA_LABEL,
    SEVERITY_SLA_DAYS,
    parse_introduced_date,
    parse_resolution_date,
    parse_severity_value,
    resolve_custom_field_ids,
    sla_breached,
)

LOGGER = logging.getLogger("sla_breach_reconcile")

DEFAULT_COMPARE_JQL = (
    'labels = "sla-breached" AND project in '
    "(AH, AM, DAT, OAA, CMA, CD, RS, ECL, CL, MKT, CSI, DX, AO, SS, VB, VP, COMS, GROW, EXP) "
    "AND status in (Done)"
)


def _sheet_breach_flags(
    issue: Dict[str, Any],
    severity_id: str,
    introduced_id: str,
) -> Tuple[bool, bool, bool, bool]:
    """
    Returns:
        breached_like_sheet — same boolean as aggregation in Apps Script / Python aggregate_issues
        math_path_ok — sev + intro + res_date + known Sev SLA
        math_breach — math_path_ok and resolved strictly after due
        has_label
    """
    fields = issue.get("fields") or {}
    labels = fields.get("labels") or []
    has_label = SLA_LABEL in labels

    sev = parse_severity_value(fields.get(severity_id))
    intro = parse_introduced_date(fields.get(introduced_id))
    res_date = parse_resolution_date(fields.get("resolutiondate"))

    math_path_ok = bool(
        sev and intro and res_date and sev in SEVERITY_SLA_DAYS
    )
    mb = False
    if math_path_ok and sev and intro and res_date:
        mb, _ = sla_breached(intro, sev, res_date)

    if math_path_ok:
        breached_sheet = mb
    else:
        breached_sheet = has_label

    return breached_sheet, math_path_ok, mb, has_label


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=(
            "Compare sheet-style SLA breach counting vs Navigator label queries / optional JQL."
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
        help="Resolved window end YYYY-MM-DD (inclusive). Default: today (UTC)",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="Timezone for deriving default --end calendar day",
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
        help="Comma-separated project keys override (same precedence as jira_compliance_daily via env)",
    )
    parser.add_argument(
        "--projects-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Project keys file (if --projects and JIRA_COMPLIANCE_PROJECTS unset)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Jira search page size",
    )
    parser.add_argument(
        "--compare-jql",
        type=str,
        default=DEFAULT_COMPARE_JQL,
        help="JQL to count separately (Navigator-style). Use \"\" to skip.",
    )
    parser.add_argument(
        "--align-compare-resolved-dates",
        action="store_true",
        help="AND the same resolved >= start / resolved < end+1 window onto --compare-jql "
        "(fairer vs sheet cumulative for that period).",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Optional CSV: each issue row with breach / label buckets",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()
    if args.verbose:
        LOGGER.setLevel(logging.DEBUG)

    load_dotenv()
    base, email, token = load_config()
    session = jira_session(base, email, token)

    tzname = os.environ.get("JIRA_COMPLIANCE_TIMEZONE", args.timezone).strip() or args.timezone
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except ImportError:
        print("Warning: zoneinfo missing; default end date uses UTC naming only.", file=sys.stderr)
        tz = None  # type: ignore[assignment]

    start_raw = (
        args.start
        or os.environ.get("JIRA_COMPLIANCE_START", "").strip()
        or "2026-02-01"
    )
    start_d = date.fromisoformat(start_raw)

    if args.end:
        end_d = date.fromisoformat(args.end)
    elif tz is not None:
        end_d = datetime.now(tz).date()
    else:
        end_d = date.today()

    if end_d < start_d:
        print("--end must be on or after --start", file=sys.stderr)
        sys.exit(2)

    resolved_lt = end_d + timedelta(days=1)
    base_scope = load_base_scope(args.metrics_json)
    project_keys = resolve_project_filter(args.projects, args.projects_file)

    probe = build_probe_jql(project_keys, base_scope)
    severity_id, introduced_id = resolve_custom_field_ids(session, base, probe)

    jql_cohort = build_jql(base_scope, start_d, resolved_lt, project_keys)
    fields = ["key", "project", "labels", "resolutiondate", severity_id, introduced_id]

    print("\n=== Sheet cohort JQL (resolved fixes used for Fixed / Breached) ===", flush=True)
    print(jql_cohort + "\n", flush=True)

    issues = fetch_all_issues_jql(
        session,
        base,
        jql_cohort,
        max_results=args.max_results,
        fields=fields,
    )
    print(f"Cohort fetch: {len(issues):,} issues resolved in [{start_d} .. {end_d}].", flush=True)

    missing_res = 0
    n_with_resolution = 0
    sheet_breached_n = 0
    math_breach_labeled = 0
    math_breach_unlabeled = 0
    fallback_label_breach = 0
    within_stale_label = 0
    rows_out: List[Dict[str, str]] = []

    for issue in issues:
        rk = issue.get("key") or ""
        pk = project_key(issue) or ""
        fields_d = issue.get("fields") or {}
        labels = fields_d.get("labels") or []

        res = parse_resolution_utc(fields_d.get("resolutiondate"))
        if res is None:
            missing_res += 1
            continue

        n_with_resolution += 1
        bs, mp_ok, mb, hl = _sheet_breach_flags(issue, severity_id, introduced_id)

        if bs:
            sheet_breached_n += 1
            if mp_ok and mb:
                if hl:
                    math_breach_labeled += 1
                else:
                    math_breach_unlabeled += 1
            elif not mp_ok:
                fallback_label_breach += 1
        elif mp_ok and hl and not mb:
            within_stale_label += 1

        if mb and hl:
            bucket = "math_breach_labeled"
        elif mb and not hl:
            bucket = "math_breach_unlabeled"
        elif bs and not mp_ok:
            bucket = "fallback_label_breach_no_math_path"
        elif not bs and mp_ok and hl:
            bucket = "stale_or_wrong_label_within_math"
        else:
            bucket = "within_sla_sheet"

        rows_out.append(
            {
                "issue": rk,
                "project": pk,
                "sheet_breached": "yes" if bs else "no",
                "math_path_ok": "yes" if mp_ok else "no",
                "math_breach": "yes" if mb else "no",
                "has_sla_label": "yes" if hl else "no",
                "bucket": bucket,
                "labels": " ".join(sorted(str(x) for x in labels)),
            }
        )

    print("\n=== Sheet semantics (matches JiraComplianceDaily aggregation) ===", flush=True)
    print(
        "Breached = SLA date math when severity+introduced+resolution present; "
        "else sla-breached label.",
        flush=True,
    )
    print(f"  Issues in cohort (has resolution):     {n_with_resolution:,}", flush=True)
    print(f"  Sheet-style Breached:                    {sheet_breached_n:,}", flush=True)
    print(f"    └ math breach + labeled:               {math_breach_labeled:,}", flush=True)
    print(f"    └ math breach + NO label (gap vs Nav): {math_breach_unlabeled:,}", flush=True)
    print(f"    └ breach via label fallback only:      {fallback_label_breach:,}", flush=True)
    print(f"  Within SLA but still has sla-breached *: {within_stale_label:,}", flush=True)
    print(
        "    (* stale automation, human label, or rule mismatch)",
        flush=True,
    )
    if missing_res:
        print(f"  (skipped, no resolutiondate: {missing_res})", flush=True)

    print("\n=== Why Navigator label count differs ===", flush=True)
    print(
        "• Sheet counts breaches from **vuln** fixes (SCA/SAST + fixable + not ignored) "
        "in the resolved **date window**, mostly via **calendar SLA math**.\n"
        "• Navigator `labels = sla-breached` counts **issues with that label**; "
        "many math-breach issues never ran through label automation.",
        flush=True,
    )

    raw_compare = (args.compare_jql or "").strip()
    if raw_compare:
        compare_jql = raw_compare
        if args.align_compare_resolved_dates:
            lt_s = resolved_lt.strftime("%Y-%m-%d")
            gte_s = start_d.strftime("%Y-%m-%d")
            compare_jql = (
                f"({compare_jql}) AND resolved >= \"{gte_s}\" AND resolved < \"{lt_s}\""
            )
        print("\n=== Optional compare JQL (label / status heuristic) ===", flush=True)
        print(compare_jql + "\n", flush=True)
        try:
            n_cmp = count_jql_issues(session, base, compare_jql, args.max_results)
            print(f"  Issue keys returned (paginated count): {n_cmp:,}", flush=True)
            approx = approximate_jql_count(session, base, compare_jql)
            if approx is not None:
                print(f"  Approximate-count API:               {approx:,}", flush=True)
        except Exception as exc:
            print(f"  Compare-JQL failed: {exc}", file=sys.stderr, flush=True)
        if not args.align_compare_resolved_dates:
            print(
                "\n  Tip: re-run with --align-compare-resolved-dates so this JQL uses the "
                "same resolved window as the sheet cohort.",
                flush=True,
            )

    if args.csv_out:
        fieldnames = list(rows_out[0].keys()) if rows_out else [
            "issue",
            "project",
            "sheet_breached",
            "math_path_ok",
            "math_breach",
            "has_sla_label",
            "bucket",
            "labels",
        ]
        args.csv_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in rows_out:
                w.writerow(row)
        print(f"\nWrote {args.csv_out} ({len(rows_out)} rows)", flush=True)


if __name__ == "__main__":
    main()