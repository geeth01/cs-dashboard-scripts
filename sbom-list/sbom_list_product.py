#!/usr/bin/env python3
"""
Export deduplicated CycloneDX library component list for one Jira product (mapping row).

Example (AH = contentstack-automations):
  python3 sbom_list_product.py --jira-project AH -o sbom_list_AH.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from sbom_report import (
    DEFAULT_SNYK_BASE,
    build_session,
    cyclone_library_rows,
    fetch_project_sbom,
    iter_jsonapi_resources,
    load_mappings,
    product_label,
    project_matches_repos,
    resolve_org_id,
)

LOGGER = logging.getLogger("sbom_list_product")


def find_mapping(
    mappings: List[Dict[str, Any]], jira_project: str
) -> Optional[Dict[str, Any]]:
    target = jira_project.strip().upper()
    for row in mappings:
        jp = (row.get("jiraProject") or "").strip().upper()
        if jp == target:
            return row
    return None


def unique_components_for_mapping(
    session,
    base: str,
    org_id: str,
    projects: List[Dict[str, Any]],
) -> Dict[str, Dict[str, str]]:
    """
    Union of library components across projects, keyed by group|name|version.
    """
    by_key: Dict[str, Dict[str, str]] = {}
    for proj in projects:
        pid = proj.get("id")
        if not pid:
            continue
        doc, status = fetch_project_sbom(session, base, org_id, str(pid))
        if doc is None:
            LOGGER.warning(
                "SBOM skipped for project %s (HTTP %s)",
                pid,
                status if status is not None else "error",
            )
            continue
        for row in cyclone_library_rows(doc):
            key = f"{row['group']}|{row['name']}|{row['version']}"
            if key not in by_key:
                by_key[key] = {
                    "group": row["group"],
                    "name": row["name"],
                    "version": row["version"],
                }
    return by_key


def run(
    mappings_path: str,
    jira_project: str,
    output_path: str,
    env_path: Optional[str],
) -> None:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    token = os.environ.get("SNYK_TOKEN")
    if not token:
        LOGGER.error("SNYK_TOKEN is not set (check your .env).")
        sys.exit(1)

    base = os.environ.get("SNYK_API_BASE", DEFAULT_SNYK_BASE).rstrip("/")
    session = build_session(token)

    mappings = load_mappings(mappings_path)
    row = find_mapping(mappings, jira_project)
    if not row:
        LOGGER.error("No mapping found for jiraProject=%r", jira_project)
        sys.exit(1)

    snyk_org = row.get("snykOrg")
    if not snyk_org:
        LOGGER.error("Mapping row missing snykOrg: %s", row)
        sys.exit(1)

    try:
        org_id = resolve_org_id(session, base, str(snyk_org))
    except LookupError as e:
        LOGGER.error("%s", e)
        sys.exit(1)

    projects = [
        p
        for p in iter_jsonapi_resources(session, base, f"orgs/{org_id}/projects")
        if project_matches_repos(p, list(row.get("repos") or []))
    ]

    if not projects:
        LOGGER.warning("No projects for org %s (after repo filter).", snyk_org)

    components = unique_components_for_mapping(session, base, org_id, projects)
    label = product_label(row)

    fieldnames = ["Product", "Group", "Name", "Version"]
    sorted_rows = sorted(
        components.values(),
        key=lambda r: (r["group"].lower(), r["name"].lower(), r["version"].lower()),
    )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted_rows:
            w.writerow(
                {
                    "Product": label,
                    "Group": r["group"],
                    "Name": r["name"],
                    "Version": r["version"],
                }
            )

    LOGGER.info(
        "Wrote %s (%s unique library components for %s)",
        output_path,
        len(sorted_rows),
        label,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(
        description="Export deduplicated SBOM library list for one Jira product"
    )
    p.add_argument(
        "--jira-project",
        default="AH",
        help="Jira project key from mappings.json (default: AH)",
    )
    p.add_argument(
        "--mappings",
        default="mappings.json",
        help="Path to mappings JSON (default: mappings.json)",
    )
    p.add_argument(
        "--output",
        "-o",
        default="sbom_list_AH.csv",
        help="Output CSV path (default: sbom_list_AH.csv)",
    )
    p.add_argument(
        "--env",
        default=None,
        help="Path to .env file (default: search via python-dotenv)",
    )
    args = p.parse_args()
    run(args.mappings, args.jira_project, args.output, args.env)


if __name__ == "__main__":
    main()
