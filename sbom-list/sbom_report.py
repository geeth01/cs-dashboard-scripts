#!/usr/bin/env python3
"""
SBOM CSV report: per mapping row, aggregate Snyk CycloneDX library components
(total per project and unique across projects) and open package_vulnerability issues
(issue count and distinct vulnerable packages from coordinates).

SBOM dedupe key: CycloneDX component group|name|version for components with type "library".
SLA Breach: placeholder until wired to Jira or Snyk SLA fields (see SLA_BREACH_PLACEHOLDER).
Vuln counts: org-wide when repos is empty; when repos lists specific GitHub paths, only
issues linked to the matching Snyk projects are counted (same scope as SBOM for that row).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

# API versions (Snyk REST)
API_VERSION = os.environ.get("SNYK_API_VERSION", "2024-10-15")
SBOM_API_VERSION = os.environ.get("SNYK_SBOM_API_VERSION", "2024-08-22")

DEFAULT_SNYK_BASE = "https://api.snyk.io/rest"
SLA_BREACH_PLACEHOLDER = "N/A"

LOGGER = logging.getLogger("sbom_report")


def build_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"token {token}",
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json, application/json",
        }
    )
    return s


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    max_retries: int = 6,
) -> requests.Response:
    backoff = 2.0
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = session.request(
                method, url, params=params, headers=headers, timeout=120
            )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                LOGGER.warning("Rate limited (429), waiting %.1fs before retry", wait)
                time.sleep(wait)
                backoff = min(backoff * 2, 120)
                continue
            return resp
        except (requests.RequestException, OSError) as e:
            last_exc = e
            LOGGER.warning("Request error: %s (attempt %s)", e, attempt + 1)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
    if last_exc:
        raise last_exc
    raise RuntimeError("request_with_retry failed without response")


def iter_jsonapi_pages(
    session: requests.Session,
    first_url: str,
    first_params: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield each JSON:API page body (for links.next)."""
    url = first_url
    params = first_params
    while url:
        resp = request_with_retry(session, "GET", url, params=params)
        resp.raise_for_status()
        body = resp.json()
        yield body
        next_link = (body.get("links") or {}).get("next")
        if not next_link:
            break
        # Next link is typically absolute; handle relative paths
        if next_link.startswith("http"):
            url = next_link
        else:
            parsed = urlparse(first_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            url = urljoin(base + "/", next_link.lstrip("/"))
        params = None


def iter_jsonapi_resources(
    session: requests.Session,
    base: str,
    path: str,
    *,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    params: Dict[str, Any] = {"version": API_VERSION, "limit": 100}
    if extra_params:
        params.update(extra_params)
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    for page in iter_jsonapi_pages(session, url, params):
        for item in page.get("data") or []:
            if isinstance(item, dict):
                yield item


def resolve_org_id(session: requests.Session, base: str, org_slug_or_name: str) -> str:
    target = org_slug_or_name.strip().lower()
    for org in iter_jsonapi_resources(session, base, "/orgs"):
        attrs = org.get("attributes") or {}
        slug = (attrs.get("slug") or "").strip().lower()
        name = (attrs.get("name") or "").strip().lower()
        if slug == target or name == target:
            return str(org["id"])
    raise LookupError(
        f"Snyk organization not found for {org_slug_or_name!r} "
        f"(matched against slug/name, case-insensitive)."
    )


def _project_match_haystack(attrs: Dict[str, Any]) -> str:
    """Concatenate Snyk project fields that may contain repo paths (snake + camel)."""
    keys = (
        "name",
        "target_reference",
        "target_file",
        "origin",
        "remote_repo_url",
        "remote_repo_full_name",
        "browse_url",
        "targetReference",
        "targetFile",
        "remoteRepoUrl",
    )
    parts: List[str] = []
    for k in keys:
        v = attrs.get(k)
        if v is not None and v != "":
            parts.append(str(v))
    return " ".join(parts).lower()


def _repo_match_patterns(repo: str) -> List[str]:
    """
    Snyk project metadata may use org/repo, repo slug only, or hyphenated paths.
    Try full string, last path segment, and / replaced by - or _.
    """
    rs = repo.strip().lower()
    if not rs:
        return []
    out: List[str] = [rs]
    if "/" in rs:
        last = rs.rsplit("/", 1)[-1]
        if last:
            out.append(last)
        out.append(rs.replace("/", "-"))
        out.append(rs.replace("/", "_"))
    # Dedupe preserving order
    seen: Set[str] = set()
    uniq: List[str] = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def project_matches_repos(project: Dict[str, Any], repos: List[str]) -> bool:
    """
    If repos is empty: include all Snyk projects in the org (no filter).

    If repos has entries: only include projects that match those strings. Flexible
    path/slug matching (extra attributes + org/repo variants) runs only in that case —
    not when repos is empty.
    """
    if not repos:
        return True
    return _project_matches_explicit_repo_list(project, repos)


def _project_matches_explicit_repo_list(project: Dict[str, Any], repos: List[str]) -> bool:
    attrs = project.get("attributes") or {}
    hay = _project_match_haystack(attrs)
    for r in repos:
        for pat in _repo_match_patterns(r):
            if pat and pat in hay:
                return True
    return False


def cyclone_library_rows(sbom: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    CycloneDX components with type 'library' as rows (group, name, version).
    """
    rows: List[Dict[str, str]] = []
    for comp in sbom.get("components") or []:
        if not isinstance(comp, dict):
            continue
        if (comp.get("type") or "").lower() != "library":
            continue
        group = (comp.get("group") or "").strip()
        name = (comp.get("name") or "").strip()
        version = (comp.get("version") or "").strip()
        if not name:
            continue
        rows.append({"group": group, "name": name, "version": version})
    return rows


def cyclone_library_keys(sbom: Dict[str, Any]) -> Set[str]:
    """Dedupe key: group|name|version for CycloneDX components with type 'library'."""
    return {f"{r['group']}|{r['name']}|{r['version']}" for r in cyclone_library_rows(sbom)}


def fetch_project_sbom(
    session: requests.Session,
    base: str,
    org_id: str,
    project_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    url = f"{base.rstrip('/')}/orgs/{org_id}/projects/{project_id}/sbom"
    params = {
        "version": SBOM_API_VERSION,
        "format": "cyclonedx1.4+json",
    }
    # CycloneDX JSON is not JSON:API; override session Accept.
    resp = request_with_retry(
        session,
        "GET",
        url,
        params=params,
        headers={"Accept": "application/json"},
    )
    if resp.status_code in (403, 404):
        return None, resp.status_code
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        return None, resp.status_code
    try:
        return resp.json(), resp.status_code
    except json.JSONDecodeError:
        LOGGER.warning("SBOM for project %s: response is not JSON", project_id)
        return None, resp.status_code


def aggregate_sbom_library_keys(
    session: requests.Session,
    base: str,
    org_id: str,
    projects: List[Dict[str, Any]],
) -> Tuple[int, int]:
    """
    Returns (total_library_components, unique_library_components).
    Total sums per-project library counts (same component in multiple projects counts multiple times).
    Unique is the union size across projects (group|name|version).
    """
    all_keys: Set[str] = set()
    total_components = 0
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
        keys = cyclone_library_keys(doc)
        total_components += len(keys)
        all_keys |= keys
    return total_components, len(all_keys)


def _packages_from_coordinate(coord: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for rep in coord.get("representations") or []:
        if not isinstance(rep, dict):
            continue
        dep = rep.get("dependency")
        if isinstance(dep, dict):
            name = dep.get("package_name")
            ver = dep.get("package_version")
            if name and ver is not None:
                out.add(f"{str(name).lower()}@{str(ver)}")
    return out


def issue_project_id(issue: Dict[str, Any]) -> Optional[str]:
    """Resolve Snyk issue -> project id (several JSON:API shapes)."""
    rel = issue.get("relationships") or {}
    proj = rel.get("project") or {}
    data = proj.get("data")
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    for rel_key in ("scan_item", "test", "test_result"):
        block = rel.get(rel_key) or {}
        d = block.get("data")
        if isinstance(d, dict) and d.get("id") and d.get("type") == "project":
            return str(d["id"])
        if isinstance(d, list):
            for item in d:
                if (
                    isinstance(item, dict)
                    and item.get("id")
                    and item.get("type") == "project"
                ):
                    return str(item["id"])
    attrs = issue.get("attributes") or {}
    for key in ("project_id", "projectId", "proj_id"):
        v = attrs.get(key)
        if v is not None and v != "":
            return str(v)
    return None


def list_issues_for_project_safe(
    session: requests.Session,
    base: str,
    org_id: str,
    project_id: str,
    extra_params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """GET .../projects/{id}/issues — empty list if endpoint returns 404."""
    path = f"orgs/{org_id}/projects/{project_id}/issues"
    try:
        return list(
            iter_jsonapi_resources(session, base, path, extra_params=extra_params)
        )
    except requests.HTTPError as e:
        rsp = getattr(e, "response", None)
        if rsp is not None and rsp.status_code == 404:
            return []
        raise


def collect_vulnerable_package_data(
    issues: List[Dict[str, Any]],
    allowed_project_ids: Optional[Set[str]],
) -> Tuple[int, int, List[str]]:
    """
    Count open package_vulnerability issues and distinct package@version from coordinates.
    If allowed_project_ids is set, only issues whose project id is in the set are counted
    (same scope as filtered SBOM when mappings use repos). If None, all issues count.
    """
    pkgs: Set[str] = set()
    issue_count = 0
    for issue in issues:
        if allowed_project_ids is not None:
            ipid = issue_project_id(issue)
            if not ipid or ipid not in allowed_project_ids:
                continue
        attrs = issue.get("attributes") or {}
        if attrs.get("ignored") is True:
            continue
        issue_count += 1
        found_pkg = False
        for coord in attrs.get("coordinates") or []:
            if not isinstance(coord, dict):
                continue
            for p in _packages_from_coordinate(coord):
                pkgs.add(p)
                found_pkg = True
        if not found_pkg:
            title = attrs.get("title") or attrs.get("key") or issue.get("id")
            if title is not None and str(title).strip():
                pkgs.add(f"(no package coordinates) {str(title)[:500]}")
            else:
                LOGGER.debug(
                    "Issue %s: no dependency coordinates in representations",
                    issue.get("id"),
                )
    pkg_list = sorted(pkgs)
    return issue_count, len(pkgs), pkg_list


def distinct_vulnerable_packages(
    session: requests.Session,
    base: str,
    org_id: str,
    allowed_project_ids: Optional[Set[str]] = None,
) -> Tuple[int, int]:
    """
    Open package_vulnerability issues. If allowed_project_ids is set, prefer per-project
    GET .../projects/{id}/issues (reliable). If that yields nothing, filter org issues
    (optionally with include=project). Last resort: org-wide counts with a warning.
    """
    extra = {
        "type": "package_vulnerability",
        "status": "open",
        "ignored": "false",
    }
    path = f"orgs/{org_id}/issues"
    if not allowed_project_ids:
        issues = list(iter_jsonapi_resources(session, base, path, extra_params=extra))
        ic, dp, _ = collect_vulnerable_package_data(issues, None)
        return ic, dp

    merged: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for pid in sorted(allowed_project_ids):
        for issue in list_issues_for_project_safe(session, base, org_id, pid, extra):
            iid = issue.get("id")
            if iid and iid not in seen:
                seen.add(iid)
                merged.append(issue)
    if merged:
        ic, dp, _ = collect_vulnerable_package_data(merged, None)
        return ic, dp

    extra_inc = dict(extra)
    extra_inc["include"] = "project"
    try:
        issues_inc = list(
            iter_jsonapi_resources(session, base, path, extra_params=extra_inc)
        )
    except requests.HTTPError:
        issues_inc = []
    ic, dp, _ = collect_vulnerable_package_data(issues_inc, allowed_project_ids)
    if ic > 0:
        return ic, dp

    LOGGER.warning(
        "Could not scope issues to projects %s (relationships may be missing). "
        "Using org-wide vulnerability counts for org %s.",
        allowed_project_ids,
        org_id,
    )
    issues_fallback = issues_inc
    if not issues_fallback:
        issues_fallback = list(
            iter_jsonapi_resources(session, base, path, extra_params=extra)
        )
    ic, dp, _ = collect_vulnerable_package_data(issues_fallback, None)
    return ic, dp


def product_label(row: Dict[str, Any]) -> str:
    if row.get("productName"):
        return str(row["productName"]).strip()
    jp = row.get("jiraProject") or ""
    so = row.get("snykOrg") or ""
    return f"{jp} / {so}".strip(" /")


def load_mappings(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("mappings file must be a JSON array")
    return [x for x in data if isinstance(x, dict)]


def run_report(
    mappings_path: str,
    output_path: str,
    env_path: Optional[str],
    snapshot_date: Optional[str],
    append: bool,
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

    snap = (snapshot_date or "").strip()
    if not snap:
        snap = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows_out: List[Dict[str, Any]] = []
    mappings = load_mappings(mappings_path)

    for row in mappings:
        snyk_org = row.get("snykOrg")
        if not snyk_org:
            LOGGER.error("Mapping row missing snykOrg: %s", row)
            continue
        try:
            org_id = resolve_org_id(session, base, str(snyk_org))
        except LookupError as e:
            LOGGER.error("%s", e)
            rows_out.append(
                {
                    "Snapshot_Date": snap,
                    "Product": product_label(row),
                    "Total SBOM Count": "",
                    "Unique SBOM Count": "",
                    "Vuln Issue Count": "",
                    "Unique Vuln Count": "",
                    "SLA Breach": SLA_BREACH_PLACEHOLDER,
                }
            )
            continue

        projects = [
            p
            for p in iter_jsonapi_resources(session, base, f"orgs/{org_id}/projects")
            if project_matches_repos(p, list(row.get("repos") or []))
        ]

        repos = list(row.get("repos") or [])
        if repos and not projects:
            LOGGER.warning(
                "No Snyk projects matched repos for %s (org %s); SBOM counts are 0; "
                "vulnerability counts use org-wide.",
                product_label(row),
                snyk_org,
            )
        elif not projects:
            LOGGER.warning("No projects for org %s (after repo filter).", snyk_org)

        total_sbom, unique_sbom = aggregate_sbom_library_keys(
            session, base, org_id, projects
        )
        # Empty set is falsy (org-wide vulns); only scope when repos listed and some project matched.
        allowed_issue_projects: Optional[Set[str]] = None
        if repos and projects:
            allowed_issue_projects = {str(p["id"]) for p in projects if p.get("id")}
        try:
            vuln_issues, unique_vuln_pkgs = distinct_vulnerable_packages(
                session, base, org_id, allowed_issue_projects
            )
        except requests.HTTPError as e:
            LOGGER.error("Failed to list issues for org %s: %s", snyk_org, e)
            vuln_issues, unique_vuln_pkgs = "", ""

        rows_out.append(
            {
                "Snapshot_Date": snap,
                "Product": product_label(row),
                "Total SBOM Count": total_sbom,
                "Unique SBOM Count": unique_sbom,
                "Vuln Issue Count": vuln_issues,
                "Unique Vuln Count": unique_vuln_pkgs,
                "SLA Breach": SLA_BREACH_PLACEHOLDER,
            }
        )

    fieldnames = [
        "Snapshot_Date",
        "Product",
        "Total SBOM Count",
        "Unique SBOM Count",
        "Vuln Issue Count",
        "Unique Vuln Count",
        "SLA Breach",
    ]
    write_header = not (append and os.path.isfile(output_path))
    mode = "a" if append else "w"
    with open(output_path, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        for r in rows_out:
            w.writerow(r)

    LOGGER.info("Wrote %s (%s rows)", output_path, len(rows_out))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="SBOM summary CSV from Snyk + mappings.json")
    p.add_argument(
        "--mappings",
        default="mappings.json",
        help="Path to mappings JSON (default: mappings.json)",
    )
    p.add_argument(
        "--output",
        "-o",
        default="sbom_report.csv",
        help="Output CSV path (default: sbom_report.csv)",
    )
    p.add_argument(
        "--env",
        default=None,
        help="Path to .env file (default: search via python-dotenv)",
    )
    p.add_argument(
        "--snapshot-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Date label for this run (default: today UTC). Stored in Snapshot_Date column.",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Append rows to CSV (writes header only if file does not exist)",
    )
    args = p.parse_args()
    run_report(args.mappings, args.output, args.env, args.snapshot_date, args.append)


if __name__ == "__main__":
    main()
