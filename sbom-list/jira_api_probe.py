#!/usr/bin/env python3
"""
Jira REST connectivity + issue visibility diagnostics.

Use this when ``jira_compliance_daily`` / enhanced ``search/jql`` returns **zero** issues while
Navigator shows plenty for similar JQL. Almost always caused by:

- API token belonging to another Atlassian identity (different from the Navigator user), or
- **Scoped API token** missing ``read:jira-work`` / ``read:jira-user`` / issue read entitlement, or
- No **Browse Projects** permission on portfolio keys.

This script reads the same credentials as ``.env``: ``JIRA_BASE_URL``, ``JIRA_EMAIL``, ``JIRA_API_TOKEN``.

  python3 jira_api_probe.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from jira_compliance_daily import (
    DEFAULT_METRICS_JSON,
    approximate_jql_count,
    jira_session,
    load_base_scope,
    load_config,
    parse_projects_from_base_scope,
    search_jql_page,
)


def _one_page_keys(session, base: str, jql: str) -> Tuple[int, List[str]]:
    try:
        data = search_jql_page(session, base, jql, 5, None, fields=["key"])
    except Exception as exc:
        return -1, [str(exc)]
    keys = []
    for it in data.get("issues") or []:
        k = it.get("key")
        if k:
            keys.append(str(k))
    return len(keys), keys


def main() -> None:
    base, email, token = load_config()
    session = jira_session(base, email, token)

    print("=== Config sanity ===")
    print(f"JIRA_BASE_URL (effective): {base!r}")
    print(f"JIRA_EMAIL: {email!r}")
    print(f"JIRA_API_TOKEN length: {len(token)} chars")

    print("\n=== GET /rest/api/3/myself ===")
    r = session.get(f"{base}/rest/api/3/myself")
    print(f"HTTP {r.status_code}")
    if not r.ok:
        print(r.text[:800])
        print(
            "\nFix: regenerate token at https://id.atlassian.com/manage-profile/security/api-tokens "
            "while logged into the SAME account you use in Jira Navigator; put email + token in .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    me: Dict[str, Any] = r.json()
    print(
        json.dumps(
            {
                "accountId": me.get("accountId"),
                "emailAddress": me.get("emailAddress"),
                "displayName": me.get("displayName"),
                "locale": me.get("locale"),
            },
            indent=2,
        )
    )
    my_email = (me.get("emailAddress") or "").strip().lower()
    env_email_l = email.strip().lower()
    if env_email_l and my_email and env_email_l != my_email:
        print(
            f"\nWarning: .env email {email!r} differs from REST /myself {me.get('emailAddress')!r}. "
            "Use the login that matches Navigator, or regenerate the token for that account.",
            file=sys.stderr,
        )

    mj_raw = os.environ.get("JIRA_METRICS_JSON", "").strip()
    mj_path = (
        Path(mj_raw).expanduser().resolve()
        if mj_raw
        else DEFAULT_METRICS_JSON.expanduser().resolve()
    )
    try:
        scope = load_base_scope(mj_path)
        plist = parse_projects_from_base_scope(scope)
        first_proj = plist[0] if plist else "AH"
    except Exception:
        scope = "(could not load jira_metrics_queries.json)"
        first_proj = "AH"

    probes: List[Tuple[str, str]] = [
        (
            "updated >= -30d ORDER BY updated DESC",
            "Any issue you can browse (last 30d activity)",
        ),
        ('project = AH ORDER BY updated DESC', "Project AH alone"),
        ("project = AO ORDER BY updated DESC", "Project AO alone"),
        (
            'labels = "vulnerability/fixable" AND updated >= -30d ORDER BY updated DESC',
            "Fixable vuln-ish label slice (recent activity)",
        ),
        (
            f'labels = "vulnerability/sca" AND labels = "vulnerability/fixable" '
            f'AND project = {first_proj} AND updated >= -30d ORDER BY updated DESC',
            f"Thin vuln slice on first base_scope project ({first_proj})",
        ),
    ]

    print("\n=== POST /rest/api/3/search/jql smoke tests (limit 5) ===")
    for jql_q, hint in probes:
        n, keys = _one_page_keys(session, base, jql_q)
        approx = approximate_jql_count(session, base, jql_q)
        ap_s = f", approximate-count={approx:,}" if approx is not None else ""
        prefix = "-- " + hint + " --" if hint else ""
        if prefix:
            print(f"\n{prefix}")
        print(f"JQL: {jql_q[:200]}{'…' if len(jql_q) > 200 else ''}")
        if n < 0:
            print(f"  FAILED: {keys[0]}")
        else:
            print(f"  first-page keys pulled: {n}{ap_s}  examples: {keys[:5]}")

    print("\n=== portfolio base_scope excerpt ===")
    print(scope[:260] + ("…" if len(scope) > 260 else ""))

    print("\n=== Interpret ===")
    print(
        "- If EVERY probe returns **0 keys** → this token/account cannot Browse issues REST sees. "
        "Fix token user, scopes, or site subscription; compare /myself email to Navigator profile.\n"
        "- If **global** probe returns keys but **project = X** is 0 → no Browse on that project.\n"
        "- If project probes work but full compliance JQL is 0 → narrow JQL in Navigator as same user."
    )


if __name__ == "__main__":
    main()
