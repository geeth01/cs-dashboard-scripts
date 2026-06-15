/**
 * SBOM — full Snyk logic (replaces sbom_report.py + sbom_list_product.py).
 *
 * Prerequisites:
 *   File > Project settings > Script properties: add SNYK_TOKEN = your Snyk API token.
 *
 * Optional script properties:
 *   SNYK_API_BASE (default https://api.snyk.io/rest)
 *   SNYK_API_VERSION (default 2024-10-15)
 *   SNYK_SBOM_API_VERSION (default 2024-08-22)
 *
 * Mappings: edit MAPPINGS below (same shape as mappings.json + sbomListSheet per row).
 * Sheets: SBOM_REPORT; each sbomListSheet: SBOM in A–C; vuln list in column F and again below SBOM.
 * Use SBOM → Refresh SBOM report to update report and all list tabs (incl. vulns).
 */

var SBOM_CONFIG = {
  reportSheetName: 'SBOM_REPORT',
  /** Both link to the product list sheet; ranges scroll to SBOM (A) vs vuln details (F) */
  hyperlinkColumnHeader: 'Unique SBOM Count',
  hyperlinkVulnColumnHeader: 'Unique Vuln Count',
  hyperlinkSbomRange: 'A1',
  hyperlinkVulnRange: 'F1',
  /** Blank rows between SBOM block and the “below” vuln copy */
  vulnBlankRowsBeforeBelowBlock: 1,
  /** Title row text for the vuln package list (F column + block below SBOM) */
  vulnSectionTitle: 'Vulnerable packages (org-wide, open issues)',
  slaColumn: 'SLA Breach',
};

/**
 * Same rows as mappings.json — keep in sync. Each row has sbomListSheet: tab name for
 * that product’s deduped SBOM list. MKT on marketplace vs ghost uses SBOM_LIST_MKT vs
 * SBOM_LIST_MKT_GHOST.
 */
var MAPPINGS = [
  {
    snykOrg: 'contentstack-automations',
    jiraProject: 'AH',
    repos: [],
    sbomListSheet: 'SBOM_LIST_AH',
  },
  {
    snykOrg: 'contentstack-dam',
    jiraProject: 'AM',
    repos: [],
    sbomListSheet: 'SBOM_LIST_AM',
  },
  {
    snykOrg: 'contentstack-dataengineering',
    jiraProject: 'DAT',
    repos: [],
    sbomListSheet: 'SBOM_LIST_DAT',
  },
  {
    snykOrg: 'contentstack-org-admin',
    jiraProject: 'OAA',
    repos: [],
    sbomListSheet: 'SBOM_LIST_OAA',
  },
  {
    snykOrg: 'contentstack-genies',
    jiraProject: 'CMA',
    repos: [],
    sbomListSheet: 'SBOM_LIST_CMA',
  },
  {
    snykOrg: 'contentstack-cda',
    jiraProject: 'CD',
    repos: [],
    sbomListSheet: 'SBOM_LIST_CD',
  },
  {
    snykOrg: 'contentstack-reignite',
    jiraProject: 'RS',
    repos: [],
    sbomListSheet: 'SBOM_LIST_RS',
  },
  {
    snykOrg: 'contentstack-eclipse',
    jiraProject: 'ECL',
    repos: [],
    sbomListSheet: 'SBOM_LIST_ECL',
  },
  {
    snykOrg: 'contentstack-contentfly',
    jiraProject: 'CL',
    repos: [],
    sbomListSheet: 'SBOM_LIST_CL',
  },
  {
    snykOrg: 'contentstack-marketplace',
    jiraProject: 'MKT',
    repos: [],
    sbomListSheet: 'SBOM_LIST_MKT',
  },
  {
    snykOrg: 'contentstack-datascience',
    jiraProject: 'CSI',
    repos: [],
    sbomListSheet: 'SBOM_LIST_CSI',
  },
  {
    snykOrg: 'contentstack-devex',
    jiraProject: 'DX',
    repos: [],
    sbomListSheet: 'SBOM_LIST_DX',
  },
  {
    snykOrg: 'contentstack-polaris',
    jiraProject: 'AO',
    repos: [],
    sbomListSheet: 'SBOM_LIST_AA',
  },
  {
    snykOrg: 'contentstack-superadmin',
    jiraProject: 'SS',
    repos: [],
    sbomListSheet: 'SBOM_LIST_SS',
  },
  {
    snykOrg: 'contentstack-ghost',
    jiraProject: 'MKT',
    repos: [
      'contentstack/delivery-sdk-plugins',
      'contentstack/marketplace-image-preset-builder-app',
    ],
    sbomListSheet: 'SBOM_LIST_MKT_GHOST',
  },
  {
    snykOrg: 'contentstack-ghost',
    jiraProject: 'VB',
    repos: [
      'contentstack/visual-builder',
      'contentstack/visual-editor',
      'contentstack/schema-form',
      'contentstack/live-preview-sdk',
    ],
    sbomListSheet: 'SBOM_LIST_VB',
  },
  {
    snykOrg: 'contentstack-ghost',
    jiraProject: 'VP',
    repos: [
      'contentstack/advance-broadcast-message',
      'contentstack/adv-post-message',
      'contentstack/preview-rest-api',
      'contentstack/preview-api',
      'contentstack/live-preview-sanity',
      'contentstack/release-preview-client',
      'contentstack/shopify-lp-marketplace-app',
      'contentstack/shopify-live-preview-middleware',
      'contentstack/shopify-live-preview-sdk',
    ],
    sbomListSheet: 'SBOM_LIST_VP',
  },
  {
    snykOrg: 'contentstack-ghost',
    jiraProject: 'COMS',
    repos: [
      'contentstack/composable-studio-built-in-canvas',
      'contentstack/studio-starter-app-spend-guard',
      'contentstack/studio-workflow-demo-app',
      'contentstack/use-responsive-dimension',
      'contentstack/composable-studio-api',
      'contentstack/composable-extented',
      'contentstack/composable-studio',
      'contentstack/composable-studio-sdk',
    ],
    sbomListSheet: 'SBOM_LIST_COMS',
  },
  {
    snykOrg: 'contentstack-growth',
    jiraProject: 'GROW',
    repos: [],
    sbomListSheet: 'SBOM_LIST_GROW',
  },
  {
    snykOrg: 'contentstack-venus',
    jiraProject: 'EXP',
    repos: [],
    sbomListSheet: 'SBOM_LIST_EXP',
  },
];

function setupSbomMenuOnce() {
  onOpen();
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('SBOM')
    .addItem('Refresh SBOM report (Snyk)', 'menuRefreshReport')
    .addItem('Refresh SBOM list sheets (Snyk)', 'menuRefreshListSheets')
    .addItem('Refresh report + lists + hyperlinks', 'menuRefreshAll')
    .addItem('Apply hyperlinks only (Unique SBOM + Unique Vuln)', 'menuApplyHyperlinksOnly')
    .addSeparator()
    .addItem('Create daily 9:00 AM trigger', 'menuCreateDailyTrigger')
    .addItem('Remove SBOM time triggers', 'menuRemoveTimeTriggers')
    .addToUi();
}

/** Report + every product list tab + hyperlinks (use this so list sheets get vuln column F). */
function refreshSbomEverything_() {
  refreshSbomReport_();
  refreshAllSbomListSheets_();
  applyAllSbomHyperlinks_();
}

function menuRefreshReport() {
  refreshSbomEverything_();
  SpreadsheetApp.getActive().toast(
    'SBOM_REPORT and product list sheets (incl. vulns in column F) updated.'
  );
}

function menuRefreshListSheets() {
  refreshAllSbomListSheets_();
  applyAllSbomHyperlinks_();
  SpreadsheetApp.getActive().toast('List sheets updated.');
}

function menuRefreshAll() {
  refreshSbomEverything_();
  SpreadsheetApp.getActive().toast('SBOM refresh complete.');
}

/** Re-apply links when data exists; list sheets must include vuln section for Unique Vuln links. */
function menuApplyHyperlinksOnly() {
  applyAllSbomHyperlinks_();
  SpreadsheetApp.getActive().toast('Hyperlinks applied (Unique SBOM Count + Unique Vuln Count).');
}

function menuCreateDailyTrigger() {
  removeSbomTimeTriggers_();
  ScriptApp.newTrigger('dailySbomJob')
    .timeBased()
    .atHour(9)
    .everyDays(1)
    .create();
  SpreadsheetApp.getUi().alert(
    'Daily 9:00 AM trigger created. Set time zone under File > Project settings.'
  );
}

function menuRemoveTimeTriggers() {
  removeSbomTimeTriggers_();
  SpreadsheetApp.getUi().alert('Removed SBOM time triggers.');
}

function dailySbomJob() {
  refreshSbomEverything_();
}

function removeSbomTimeTriggers_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'dailySbomJob') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
}

// --- Config & HTTP ---

function getSnykToken_() {
  var t = PropertiesService.getScriptProperties().getProperty('SNYK_TOKEN');
  if (!t || !String(t).trim()) {
    throw new Error(
      'Set Script property SNYK_TOKEN (File > Project settings > Script properties).'
    );
  }
  return String(t).trim();
}

function getApiBase_() {
  var b =
    PropertiesService.getScriptProperties().getProperty('SNYK_API_BASE') ||
    'https://api.snyk.io/rest';
  return b.replace(/\/$/, '');
}

function getApiVersion_() {
  return (
    PropertiesService.getScriptProperties().getProperty('SNYK_API_VERSION') ||
    '2024-10-15'
  );
}

function getSbomApiVersion_() {
  return (
    PropertiesService.getScriptProperties().getProperty('SNYK_SBOM_API_VERSION') ||
    '2024-08-22'
  );
}

function getJsonApiHeaders_(token) {
  return {
    Authorization: 'token ' + token,
    'Content-Type': 'application/vnd.api+json',
    Accept: 'application/vnd.api+json, application/json',
  };
}

function buildQuery_(obj) {
  var parts = [];
  for (var k in obj) {
    if (obj.hasOwnProperty(k) && obj[k] !== undefined && obj[k] !== null) {
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(obj[k])));
    }
  }
  return parts.join('&');
}

function resolveNextUrl_(firstUrl, nextLink) {
  if (!nextLink) {
    return null;
  }
  if (nextLink.indexOf('http') === 0) {
    return nextLink;
  }
  var m = firstUrl.match(/^(https?:\/\/[^\/]+)/);
  var origin = m ? m[1] : '';
  return origin + '/' + String(nextLink).replace(/^\//, '');
}

function requestWithRetry_(method, url, headers) {
  var maxRetries = 6;
  var backoff = 2000;
  var lastErr;
  headers = headers || {};
  for (var attempt = 0; attempt < maxRetries; attempt++) {
    try {
      var resp = UrlFetchApp.fetch(url, {
        method: method,
        muteHttpExceptions: true,
        headers: headers,
      });
      var code = resp.getResponseCode();
      if (code === 429) {
        var h = resp.getHeaders();
        var ra = h['Retry-After'] || h['retry-after'];
        var waitMs =
          ra && /^\d+$/.test(String(ra)) ? parseInt(ra, 10) * 1000 : backoff;
        Utilities.sleep(Math.min(waitMs, 120000));
        backoff = Math.min(backoff * 2, 120000);
        continue;
      }
      return resp;
    } catch (e) {
      lastErr = e;
      Utilities.sleep(backoff);
      backoff = Math.min(backoff * 2, 120000);
    }
  }
  throw lastErr || new Error('Request failed');
}

function iterJsonApiResources_(base, path, token, extraParams, allow404) {
  var params = { version: getApiVersion_(), limit: 100 };
  if (extraParams) {
    for (var k in extraParams) {
      if (extraParams.hasOwnProperty(k)) {
        params[k] = extraParams[k];
      }
    }
  }
  var url0 = base.replace(/\/$/, '') + '/' + path.replace(/^\//, '');
  var firstUrl = url0 + '?' + buildQuery_(params);
  var nextUrl = firstUrl;
  var out = [];
  var headers = getJsonApiHeaders_(token);
  var pageGuard = 0;
  while (nextUrl && pageGuard < 5000) {
    pageGuard++;
    var resp = requestWithRetry_('GET', nextUrl, headers);
    var code = resp.getResponseCode();
    if (code === 404 && allow404 && pageGuard === 1) {
      return [];
    }
    if (code >= 400) {
      throw new Error('Snyk API ' + code + ': ' + resp.getContentText().slice(0, 500));
    }
    var body = JSON.parse(resp.getContentText());
    var items = body.data || [];
    for (var i = 0; i < items.length; i++) {
      out.push(items[i]);
    }
    var nextLink = body.links && body.links.next ? body.links.next : null;
    if (!nextLink) {
      break;
    }
    nextUrl = resolveNextUrl_(firstUrl, nextLink);
  }
  return out;
}

// --- Snyk domain (parity with Python) ---

function resolveOrgId_(base, token, orgSlugOrName) {
  var target = String(orgSlugOrName)
    .trim()
    .toLowerCase();
  var orgs = iterJsonApiResources_(base, '/orgs', token, null);
  for (var i = 0; i < orgs.length; i++) {
    var org = orgs[i];
    var attrs = org.attributes || {};
    var slug = String(attrs.slug || '')
      .trim()
      .toLowerCase();
    var name = String(attrs.name || '')
      .trim()
      .toLowerCase();
    if (slug === target || name === target) {
      return String(org.id);
    }
  }
  throw new Error('Snyk organization not found for "' + orgSlugOrName + '"');
}

/** Concatenate Snyk project fields for substring matching (used only with explicit repos). */
function projectMatchHaystack_(attrs) {
  var keys = [
    'name',
    'target_reference',
    'target_file',
    'origin',
    'remote_repo_url',
    'remote_repo_full_name',
    'browse_url',
    'targetReference',
    'targetFile',
    'remoteRepoUrl',
  ];
  var parts = [];
  for (var k = 0; k < keys.length; k++) {
    var v = attrs[keys[k]];
    if (v !== undefined && v !== null && String(v) !== '') {
      parts.push(String(v));
    }
  }
  return parts.join(' ').toLowerCase();
}

/** Full path, last segment, and slug variants — matches Snyk project naming differences. */
function repoMatchPatterns_(repo) {
  var rs = String(repo || '')
    .trim()
    .toLowerCase();
  if (!rs) {
    return [];
  }
  var out = [rs];
  if (rs.indexOf('/') !== -1) {
    var parts = rs.split('/');
    var last = parts[parts.length - 1];
    if (last) {
      out.push(last);
    }
    out.push(rs.replace(/\//g, '-'));
    out.push(rs.replace(/\//g, '_'));
  }
  var seen = {};
  var uniq = [];
  for (var i = 0; i < out.length; i++) {
    if (out[i] && !seen[out[i]]) {
      seen[out[i]] = true;
      uniq.push(out[i]);
    }
  }
  return uniq;
}

/**
 * Empty repos: include all Snyk projects in the org (no filter).
 * Non-empty repos: include only projects that match — flexible matching runs only then.
 */
function projectMatchesRepos_(project, repos) {
  if (!repos || !repos.length) {
    return true;
  }
  return projectMatchesReposForExplicitRepoList_(project, repos);
}

/** Flexible path/slug + extra fields — only called for mappings with explicit repo lists. */
function projectMatchesReposForExplicitRepoList_(project, repos) {
  var attrs = project.attributes || {};
  var hay = projectMatchHaystack_(attrs);
  for (var i = 0; i < repos.length; i++) {
    var patterns = repoMatchPatterns_(repos[i]);
    for (var j = 0; j < patterns.length; j++) {
      if (patterns[j] && hay.indexOf(patterns[j]) !== -1) {
        return true;
      }
    }
  }
  return false;
}

function cycloneLibraryRows_(sbom) {
  var rows = [];
  var comps = sbom.components || [];
  for (var i = 0; i < comps.length; i++) {
    var comp = comps[i];
    if (!comp || typeof comp !== 'object') {
      continue;
    }
    if (String(comp.type || '')
      .toLowerCase() !== 'library') {
      continue;
    }
    var group = String(comp.group || '').trim();
    var name = String(comp.name || '').trim();
    var version = String(comp.version || '').trim();
    if (!name) {
      continue;
    }
    rows.push({ group: group, name: name, version: version });
  }
  return rows;
}

function cycloneLibraryKeys_(sbom) {
  var rows = cycloneLibraryRows_(sbom);
  var keys = {};
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    keys[r.group + '|' + r.name + '|' + r.version] = true;
  }
  return keys;
}

function fetchProjectSbom_(base, orgId, projectId, token) {
  var url =
    base +
    '/orgs/' +
    encodeURIComponent(orgId) +
    '/projects/' +
    encodeURIComponent(projectId) +
    '/sbom?' +
    buildQuery_({
      version: getSbomApiVersion_(),
      format: 'cyclonedx1.4+json',
    });
  var resp = requestWithRetry_('GET', url, {
    Authorization: 'token ' + token,
    Accept: 'application/json',
  });
  var code = resp.getResponseCode();
  if (code === 403 || code === 404) {
    return { doc: null, status: code };
  }
  if (code >= 400) {
    return { doc: null, status: code };
  }
  try {
    return { doc: JSON.parse(resp.getContentText()), status: code };
  } catch (e) {
    return { doc: null, status: code };
  }
}

function aggregateSbomLibraryKeys_(base, orgId, projects, token) {
  var allKeys = {};
  var total = 0;
  for (var i = 0; i < projects.length; i++) {
    var proj = projects[i];
    var pid = proj.id;
    if (!pid) {
      continue;
    }
    var got = fetchProjectSbom_(base, orgId, String(pid), token);
    if (!got.doc) {
      continue;
    }
    var keyMap = cycloneLibraryKeys_(got.doc);
    var n = 0;
    for (var k in keyMap) {
      if (keyMap.hasOwnProperty(k)) {
        n++;
        allKeys[k] = true;
      }
    }
    total += n;
  }
  var uniq = 0;
  for (var k2 in allKeys) {
    if (allKeys.hasOwnProperty(k2)) {
      uniq++;
    }
  }
  return { total: total, unique: uniq };
}

function packagesFromCoordinate_(coord) {
  var out = {};
  var reps = coord.representations || [];
  for (var i = 0; i < reps.length; i++) {
    var rep = reps[i];
    if (!rep || typeof rep !== 'object') {
      continue;
    }
    var dep = rep.dependency;
    if (!dep || typeof dep !== 'object') {
      continue;
    }
    var name = dep.package_name;
    var ver = dep.package_version;
    if (name && ver !== undefined && ver !== null) {
      out[String(name).toLowerCase() + '@' + String(ver)] = true;
    }
  }
  return out;
}

function issueProjectId_(issue) {
  var rel = issue.relationships || {};
  var proj = rel.project || {};
  var data = proj.data;
  if (data && typeof data === 'object' && data.id) {
    return String(data.id);
  }
  var keys = ['scan_item', 'test', 'test_result'];
  for (var ki = 0; ki < keys.length; ki++) {
    var block = rel[keys[ki]] || {};
    var d = block.data;
    if (d && typeof d === 'object' && !Array.isArray(d) && d.id && d.type === 'project') {
      return String(d.id);
    }
    if (Array.isArray(d)) {
      for (var j = 0; j < d.length; j++) {
        if (d[j] && d[j].id && d[j].type === 'project') {
          return String(d[j].id);
        }
      }
    }
  }
  var attrs = issue.attributes || {};
  if (attrs.project_id != null && attrs.project_id !== '') {
    return String(attrs.project_id);
  }
  if (attrs.projectId != null && attrs.projectId !== '') {
    return String(attrs.projectId);
  }
  if (attrs.proj_id != null && attrs.proj_id !== '') {
    return String(attrs.proj_id);
  }
  return null;
}

function vulnIssueQueryParams_() {
  return {
    type: 'package_vulnerability',
    status: 'open',
    ignored: 'false',
  };
}

/** Cache org issues list (no include) per org. */
function getOrgIssuesCached_(cache, base, orgId, token) {
  if (cache[orgId]) {
    return cache[orgId];
  }
  var issues = iterJsonApiResources_(
    base,
    'orgs/' + orgId + '/issues',
    token,
    vulnIssueQueryParams_()
  );
  cache[orgId] = issues;
  return issues;
}

function getOrgIssuesWithIncludeCached_(cache, base, orgId, token) {
  var key = orgId + '_inc';
  if (Object.prototype.hasOwnProperty.call(cache, key)) {
    return cache[key] || [];
  }
  try {
    var qp = vulnIssueQueryParams_();
    qp.include = 'project';
    var issues = iterJsonApiResources_(
      base,
      'orgs/' + orgId + '/issues',
      token,
      qp
    );
    cache[key] = issues;
    return issues;
  } catch (e) {
    cache[key] = [];
    return [];
  }
}

/**
 * allowedProjectIds: null = org-wide (repos empty). Object map id->true = same projects as SBOM.
 */
function aggregateVulnFromIssues_(issues, allowedProjectIds) {
  var pkgs = {};
  var issueCount = 0;
  for (var i = 0; i < issues.length; i++) {
    var issue = issues[i];
    if (allowedProjectIds) {
      var ipid = issueProjectId_(issue);
      if (!ipid || !allowedProjectIds[ipid]) {
        continue;
      }
    }
    var attrs = issue.attributes || {};
    if (attrs.ignored === true) {
      continue;
    }
    issueCount++;
    var coords = attrs.coordinates || [];
    var foundPkg = false;
    for (var c = 0; c < coords.length; c++) {
      var coord = coords[c];
      if (!coord || typeof coord !== 'object') {
        continue;
      }
      var pmap = packagesFromCoordinate_(coord);
      for (var p in pmap) {
        if (pmap.hasOwnProperty(p)) {
          pkgs[p] = true;
          foundPkg = true;
        }
      }
    }
    if (!foundPkg) {
      var fallback =
        attrs.title != null && String(attrs.title).trim() !== ''
          ? String(attrs.title).trim()
          : attrs.key != null && String(attrs.key).trim() !== ''
            ? String(attrs.key).trim()
            : issue.id != null
              ? String(issue.id)
              : '';
      if (fallback) {
        var line = '(no package coordinates) ' + fallback.slice(0, 500);
        pkgs[line] = true;
      }
    }
  }
  var pkgList = [];
  for (var k in pkgs) {
    if (pkgs.hasOwnProperty(k)) {
      pkgList.push(k);
    }
  }
  pkgList.sort();
  return {
    issues: issueCount,
    distinctPkgs: pkgList.length,
    packages: pkgList,
  };
}

/**
 * Per-project /issues first; then org + include=project + filter; then org-wide (warn).
 */
function computeVulnerablePackageDataFull_(base, orgId, token, allowedProjectIds, orgIssuesCache) {
  var cache = orgIssuesCache || {};
  var extra = vulnIssueQueryParams_();
  if (!allowedProjectIds) {
    var issuesAll = getOrgIssuesCached_(cache, base, orgId, token);
    return aggregateVulnFromIssues_(issuesAll, null);
  }
  var merged = [];
  var seen = {};
  for (var pid in allowedProjectIds) {
    if (!allowedProjectIds.hasOwnProperty(pid)) {
      continue;
    }
    var path =
      'orgs/' +
      orgId +
      '/projects/' +
      encodeURIComponent(pid) +
      '/issues';
    var block = iterJsonApiResources_(base, path, token, extra, true);
    for (var bi = 0; bi < block.length; bi++) {
      var issue = block[bi];
      var iid = issue.id;
      if (iid && !seen[iid]) {
        seen[iid] = true;
        merged.push(issue);
      }
    }
  }
  if (merged.length) {
    return aggregateVulnFromIssues_(merged, null);
  }
  var issuesInc = getOrgIssuesWithIncludeCached_(cache, base, orgId, token);
  var scoped = aggregateVulnFromIssues_(issuesInc, allowedProjectIds);
  if (scoped.issues > 0) {
    return scoped;
  }
  var issuesPlain = getOrgIssuesCached_(cache, base, orgId, token);
  scoped = aggregateVulnFromIssues_(issuesPlain, allowedProjectIds);
  if (scoped.issues > 0) {
    return scoped;
  }
  console.warn(
    'Scoping vulns to projects failed; using org-wide counts for org ' + orgId
  );
  return aggregateVulnFromIssues_(issuesPlain, null);
}

function collectVulnerablePackageData_(base, orgId, token, allowedProjectIds, orgIssuesCache) {
  return computeVulnerablePackageDataFull_(
    base,
    orgId,
    token,
    allowedProjectIds,
    orgIssuesCache
  );
}

function distinctVulnerablePackages_(base, orgId, token, allowedProjectIds, orgIssuesCache) {
  var d = computeVulnerablePackageDataFull_(
    base,
    orgId,
    token,
    allowedProjectIds,
    orgIssuesCache
  );
  return { issues: d.issues, distinctPkgs: d.distinctPkgs };
}

function productLabel_(row) {
  if (row.productName) {
    return String(row.productName).trim();
  }
  var jp = row.jiraProject || '';
  var so = row.snykOrg || '';
  var s = jp + ' / ' + so;
  return s.replace(/^\s*\/\s*|\s*\/\s*$/g, '').trim();
}

function snapshotDateStr_() {
  return Utilities.formatDate(
    new Date(),
    Session.getScriptTimeZone() || 'UTC',
    'yyyy-MM-dd'
  );
}

function buildReportRows_() {
  var token = getSnykToken_();
  var base = getApiBase_();
  var snap = snapshotDateStr_();
  var slaPlaceholder = 'N/A';
  var rowsOut = [];

  var orgCache = {};
  var orgIssuesCache = {};

  for (var m = 0; m < MAPPINGS.length; m++) {
    var row = MAPPINGS[m];
    var snykOrg = row.snykOrg;
    if (!snykOrg) {
      continue;
    }
    var orgId;
    try {
      var cacheKey = String(snykOrg).toLowerCase();
      if (orgCache[cacheKey]) {
        orgId = orgCache[cacheKey];
      } else {
        orgId = resolveOrgId_(base, token, String(snykOrg));
        orgCache[cacheKey] = orgId;
      }
    } catch (e) {
      rowsOut.push({
        Snapshot_Date: snap,
        Product: productLabel_(row),
        Total_SBOM: '',
        Unique_SBOM: '',
        Vuln_Issues: '',
        Unique_Vuln: '',
        SLA: slaPlaceholder,
        _error: String(e.message || e),
      });
      continue;
    }

    var allProjects = iterJsonApiResources_(
      base,
      'orgs/' + orgId + '/projects',
      token,
      null
    );
    var repos = row.repos || [];
    var projects = [];
    for (var p = 0; p < allProjects.length; p++) {
      if (projectMatchesRepos_(allProjects[p], repos)) {
        projects.push(allProjects[p]);
      }
    }

    if (repos.length && !projects.length) {
      console.warn(
        'SBOM: no Snyk projects matched repos for ' +
          productLabel_(row) +
          '; SBOM stays 0; vuln counts use org-wide (same as Python empty-set fallback).'
      );
    }

    var agg = aggregateSbomLibraryKeys_(base, orgId, projects, token);
    /** Only scope when at least one project matched — {} is truthy in JS and broke MKT ghost. */
    var allowedIssueProjects = null;
    if (repos.length && projects.length) {
      allowedIssueProjects = {};
      for (var pi = 0; pi < projects.length; pi++) {
        var pid = projects[pi].id;
        if (pid) {
          allowedIssueProjects[String(pid)] = true;
        }
      }
    }
    var vuln;
    try {
      vuln = distinctVulnerablePackages_(
        base,
        orgId,
        token,
        allowedIssueProjects,
        orgIssuesCache
      );
    } catch (e2) {
      rowsOut.push({
        Snapshot_Date: snap,
        Product: productLabel_(row),
        Total_SBOM: agg.total,
        Unique_SBOM: agg.unique,
        Vuln_Issues: '',
        Unique_Vuln: '',
        SLA: slaPlaceholder,
      });
      continue;
    }

    rowsOut.push({
      Snapshot_Date: snap,
      Product: productLabel_(row),
      Total_SBOM: agg.total,
      Unique_SBOM: agg.unique,
      Vuln_Issues: vuln.issues,
      Unique_Vuln: vuln.distinctPkgs,
      SLA: slaPlaceholder,
    });
  }

  return rowsOut;
}

function writeReportToSheet_(rowsOut) {
  var ss = SpreadsheetApp.getActive();
  var sheet = ss.getSheetByName(SBOM_CONFIG.reportSheetName);
  if (!sheet) {
    sheet = ss.insertSheet(SBOM_CONFIG.reportSheetName);
  }
  sheet.clearContents();

  var headers = [
    'Snapshot_Date',
    'Product',
    'Total SBOM Count',
    'Unique SBOM Count',
    'Vuln Issue Count',
    'Unique Vuln Count',
  ];
  var table = [headers];
  for (var i = 0; i < rowsOut.length; i++) {
    var r = rowsOut[i];
    table.push([
      r.Snapshot_Date,
      r.Product,
      r.Total_SBOM,
      r.Unique_SBOM,
      r.Vuln_Issues,
      r.Unique_Vuln,
    ]);
  }
  if (table.length) {
    sheet.getRange(1, 1, table.length, table[0].length).setValues(table);
  }
}

function refreshSbomReport_() {
  var rows = buildReportRows_();
  writeReportToSheet_(rows);
}

function uniqueComponentsForMapping_(row, token, base, orgId, projects) {
  var byKey = {};
  for (var i = 0; i < projects.length; i++) {
    var proj = projects[i];
    var pid = proj.id;
    if (!pid) {
      continue;
    }
    var got = fetchProjectSbom_(base, orgId, String(pid), token);
    if (!got.doc) {
      continue;
    }
    var libRows = cycloneLibraryRows_(got.doc);
    for (var j = 0; j < libRows.length; j++) {
      var lr = libRows[j];
      var key = lr.group + '|' + lr.name + '|' + lr.version;
      if (!byKey[key]) {
        byKey[key] = { group: lr.group, name: lr.name, version: lr.version };
      }
    }
  }
  return byKey;
}

/** Tab name for SBOM list for a report Product label (must match productLabel_ row). */
function getListSheetForProductLabel_(productLabelStr) {
  var pl = String(productLabelStr || '').trim();
  for (var i = 0; i < MAPPINGS.length; i++) {
    if (productLabel_(MAPPINGS[i]) === pl) {
      return MAPPINGS[i].sbomListSheet || null;
    }
  }
  return null;
}

/**
 * @param {Object=} orgIssuesCache orgId -> full issue list (reused across ghost rows).
 */
function fillSbomListSheetForRow_(row, orgIssuesCache) {
  if (!row || !row.sbomListSheet || !row.snykOrg) {
    return;
  }
  var tabName = row.sbomListSheet;

  var token = getSnykToken_();
  var base = getApiBase_();
  var orgId = resolveOrgId_(base, token, String(row.snykOrg));
  var allProjects = iterJsonApiResources_(
    base,
    'orgs/' + orgId + '/projects',
    token,
    null
  );
  var repos = row.repos || [];
  var projects = [];
  for (var p = 0; p < allProjects.length; p++) {
    if (projectMatchesRepos_(allProjects[p], repos)) {
      projects.push(allProjects[p]);
    }
  }

  if (repos.length && !projects.length) {
    console.warn(
      'SBOM list: no projects matched repos for ' +
        productLabel_(row) +
        '; vuln column F uses org-wide counts.'
    );
  }

  var byKey = uniqueComponentsForMapping_(row, token, base, orgId, projects);
  var label = productLabel_(row);

  var keys = [];
  for (var k in byKey) {
    if (byKey.hasOwnProperty(k)) {
      keys.push(k);
    }
  }
  keys.sort(function (a, b) {
    var xa = byKey[a];
    var xb = byKey[b];
    var ga = String(xa.group).toLowerCase();
    var gb = String(xb.group).toLowerCase();
    if (ga !== gb) {
      return ga < gb ? -1 : 1;
    }
    var na = String(xa.name).toLowerCase();
    var nb = String(xb.name).toLowerCase();
    if (na !== nb) {
      return na < nb ? -1 : 1;
    }
    var va = String(xa.version).toLowerCase();
    var vb = String(xb.version).toLowerCase();
    return va < vb ? -1 : va > vb ? 1 : 0;
  });

  var allowedIssueProjects = null;
  if (repos.length && projects.length) {
    allowedIssueProjects = {};
    for (var pi = 0; pi < projects.length; pi++) {
      var prid = projects[pi].id;
      if (prid) {
        allowedIssueProjects[String(prid)] = true;
      }
    }
  }
  var vulnData;
  try {
    vulnData = collectVulnerablePackageData_(
      base,
      orgId,
      token,
      allowedIssueProjects,
      orgIssuesCache
    );
  } catch (e) {
    console.error(
      'Vuln data failed for ' + tabName + ': ' + (e.message || e)
    );
    vulnData = { issues: 0, distinctPkgs: 0, packages: [] };
  }

  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(tabName);
  if (!sh) {
    sh = ss.insertSheet(tabName);
  }
  sh.clearContents();
  /** A–C: SBOM from row 1. F: org-wide vuln packages from F1 (parallel block, not below SBOM). */
  var sbomOut = [['Product', 'Name', 'Version']];
  for (var i = 0; i < keys.length; i++) {
    var o = byKey[keys[i]];
    sbomOut.push([label, o.name, o.version]);
  }
  if (sbomOut.length) {
    sh.getRange(1, 1, sbomOut.length, 3).setValues(sbomOut);
  }
  writeVulnerablePackagesColumnF_(sh, vulnData);
  var gap = SBOM_CONFIG.vulnBlankRowsBeforeBelowBlock || 1;
  var startBelow = sbomOut.length + gap + 1;
  writeVulnerablePackagesBelowSbom_(sh, vulnData, startBelow);
}

/**
 * Builds the 2D array for the vuln package list (title, header, then rows).
 */
function buildVulnerablePackageRows_(vulnData) {
  var pkgs = (vulnData && vulnData.packages) || [];
  var rows = [];
  rows.push([SBOM_CONFIG.vulnSectionTitle]);
  rows.push(['package@version or issue title']);
  for (var vi = 0; vi < pkgs.length; vi++) {
    rows.push([pkgs[vi]]);
  }
  return rows;
}

/**
 * Column F — Sheet.getRange(row, col, numRows, numColumns): last args are COUNTS, not end cell.
 * (1,6,n,6) was 6 columns wide → setValues failed → nothing appeared in F.
 */
function writeVulnerablePackagesColumnF_(sh, vulnData) {
  var col = buildVulnerablePackageRows_(vulnData);
  if (col.length) {
    sh.getRange(1, 6, col.length, 1).setValues(col);
  }
}

/** Same list under the SBOM table (scroll to the end of the sheet) for visibility. */
function writeVulnerablePackagesBelowSbom_(sh, vulnData, startRow) {
  var rows = buildVulnerablePackageRows_(vulnData);
  if (rows.length && startRow >= 1) {
    sh.getRange(startRow, 1, rows.length, 1).setValues(rows);
  }
}

function refreshAllSbomListSheets_() {
  ensureListSheets_();
  var orgIssuesCache = {};
  for (var i = 0; i < MAPPINGS.length; i++) {
    var row = MAPPINGS[i];
    if (!row.sbomListSheet) {
      continue;
    }
    try {
      fillSbomListSheetForRow_(row, orgIssuesCache);
    } catch (e) {
      console.error('SBOM list failed for ' + row.sbomListSheet + ': ' + e);
    }
  }
}

function ensureListSheets_() {
  var ss = SpreadsheetApp.getActive();
  for (var i = 0; i < MAPPINGS.length; i++) {
    var name = MAPPINGS[i].sbomListSheet;
    if (!name || ss.getSheetByName(name)) {
      continue;
    }
    ss.insertSheet(name);
  }
}

/**
 * Unique SBOM Count and Unique Vuln Count both use #gid= to the same product sheet
 * (SBOM in A–C; vuln list in F and repeated below SBOM in column A).
 */
function applyAllSbomHyperlinks_() {
  var ss = SpreadsheetApp.getActive();
  var sheet = ss.getSheetByName(SBOM_CONFIG.reportSheetName);
  if (!sheet) {
    return;
  }
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) {
    return;
  }
  var headers = data[0];
  var colProduct = headers.indexOf('Product');
  var colSbom = headers.indexOf(SBOM_CONFIG.hyperlinkColumnHeader);
  var colVuln = headers.indexOf(SBOM_CONFIG.hyperlinkVulnColumnHeader);
  if (colProduct === -1 || colSbom === -1) {
    return;
  }
  for (var r = 1; r < data.length; r++) {
    var productCell = String(data[r][colProduct]).trim();
    var listName = getListSheetForProductLabel_(productCell);
    if (!listName) {
      continue;
    }
    var listSheet = ss.getSheetByName(listName);
    if (!listSheet) {
      continue;
    }
    var gid = listSheet.getSheetId();
    var cellSbom = data[r][colSbom];
    var dispSbom =
      cellSbom === '' || cellSbom == null ? '0' : String(cellSbom);
    var rSbom = SBOM_CONFIG.hyperlinkSbomRange || 'A1';
    var rVuln = SBOM_CONFIG.hyperlinkVulnRange || 'F1';
    var formulaSbom =
      '=HYPERLINK("#gid=' +
      gid +
      '&range=' +
      rSbom +
      '","' +
      dispSbom.replace(/"/g, '""') +
      '")';
    sheet.getRange(r + 1, colSbom + 1).setFormula(formulaSbom);
    if (colVuln !== -1) {
      var cellVuln = data[r][colVuln];
      var dispVuln =
        cellVuln === '' || cellVuln == null ? '0' : String(cellVuln);
      var formulaVuln =
        '=HYPERLINK("#gid=' +
        gid +
        '&range=' +
        rVuln +
        '","' +
        dispVuln.replace(/"/g, '""') +
        '")';
      sheet.getRange(r + 1, colVuln + 1).setFormula(formulaVuln);
    }
  }
}
