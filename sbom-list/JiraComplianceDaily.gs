/**
 * Jira daily SLA compliance → Google Sheets (port of jira_compliance_daily.py --start 2026-02-01 -o report.csv).
 * Does NOT implement report_kpi_split / --kpi-output.
 *
 * Creates:
 *   - One tab per unique jiraProject from mappings (same keys as mappings.json, deduped).
 *   The Summary sheet is not otherwise modified; on successful completion only cell D25 is set to
 *   "Last updated: … IST" (Asia/Kolkata) if a tab named "Summary" exists.
 *
 * SETUP (Script properties):
 *   JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
 * Optional:
 *   JIRA_FIELD_SEVERITY, JIRA_FIELD_VULNERABILITY_INTRODUCED
 *   JIRA_COMPLIANCE_START          YYYY-MM-DD (default 2026-02-01)
 *   JIRA_COMPLIANCE_TIMEZONE       IANA tz (default UTC) — buckets resolutiondate to calendar days
 *   JIRA_COMPLIANCE_PROJECT_KEYS   comma-separated; overrides default list from mappings
 *   JIRA_COMPLIANCE_BASE_SCOPE     full JQL fragment; default matches jira_metrics_queries.json
 *   JIRA_COMPLIANCE_SKIP_OPEN_PAST_SLA  if "true", skips open-past-SLA Jira counts (faster); today's
 *                                      Open-Breached cell still shows HYPERLINK with 0
 *   JIRA_COMPLIANCE_OPEN_PAST_SLEEP_MS  default 0
 *
 * If you also use JiraSlaBreachLabel.gs, merge menus: see cdlOnOpen_() and call it from onOpen.
 *
 * RUN: runJiraComplianceDaily_()
 *
 * Writes each project tab as it finishes (progress without waiting for the full run). Open-past-SLA
 * counts use one shared cache across projects. Execution may still hit the ~6 min quota for very
 * large ranges — use JIRA_COMPLIANCE_SKIP_OPEN_PAST_SLA or split JIRA_COMPLIANCE_PROJECT_KEYS.
 *
 * Project sheets: values written from row 2 only; row 1 headers filled only where cells are empty.
 * Data rows are newest-first (today in row 2). Rows below the last data row are clearContent
 * (stale days). Today's row Open-Breached is a HYPERLINK to the shared Jira dashboard.
 */

var CDL_SLA_LABEL = 'sla-breached';

var CDL_SEVERITY_SLA_DAYS = {
  'Sev-0': 14,
  'Sev-1': 30,
  'Sev-2': 90,
  'Sev-3': 180
};

var CDL_INTRODUCED_NAMES = ['vulnerability_introduced_date', 'vulnerability_introduced_date[Date]'];

/** Default base_scope from jira_metrics_queries.json */
var CDL_BASE_SCOPE_DEFAULT =
  'project in (AH, AM, DAT, OAA, CMA, CD, RS, ECL, CL, MKT, CSI, DX, AO, SS, VB, VP, COMS, GROW, EXP)';

/** Unique jiraProject keys from mappings.json (order preserved). */
var CDL_MAPPING_PROJECT_KEYS_DEFAULT = [
  'AH',
  'AM',
  'DAT',
  'OAA',
  'CMA',
  'CD',
  'RS',
  'ECL',
  'CL',
  'MKT',
  'CSI',
  'DX',
  'AO',
  'SS',
  'VB',
  'VP',
  'COMS',
  'GROW',
  'EXP'
];

/** Same column headers as Python write_csv / report.csv */
var CDL_DISPLAY_HEADERS = [
  'Project',
  'Date',
  'Fixed-Total',
  'Fixed-Breached',
  'Fixed-Within_SLA',
  'Daily-Compliance',
  'Open-Breached',
  'Cumulative-Fixed-Total',
  'Cumulative-Fixed-Breached',
  'Cumulative-Fixed-Within_SLA',
  'Cumulative-Compliance'
];

var CDL_DEFAULT_START = '2026-02-01';
var CDL_OPEN_DONE_STATUSES = ['Done', 'Archived', 'Rejected'];

/** Open-Breached (today's row) links here for every project tab. */
var CDL_OPEN_BREACHED_DASHBOARD_URL = 'https://contentstack.atlassian.net/jira/dashboards/11219';

var CDL_SUMMARY_SHEET_NAME = 'Summary';
var CDL_SUMMARY_LAST_UPDATED_ROW = 25;
var CDL_SUMMARY_LAST_UPDATED_COL = 4;

/**
 * Add to onOpen (merge with JiraSlaBreachLabel.gs if needed):
 *   cdlOnOpen_();
 */
function cdlOnOpen_() {
  SpreadsheetApp.getUi()
    .createMenu('Jira compliance')
    .addItem('Refresh report (Jira → sheets)', 'runJiraComplianceDaily_')
    .addItem('Show latest open counts', 'showLatestOpenCounts_')
    .addItem('Backfill historical Open-Breached (one-time)', 'runBackfillOpenBreached_')
    .addToUi();
}

function showLatestOpenCounts_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var projectKeys = CDL_MAPPING_PROJECT_KEYS_DEFAULT.slice();
  var props = PropertiesService.getScriptProperties();
  var custom = props.getProperty('JIRA_COMPLIANCE_PROJECT_KEYS');
  if (custom && String(custom).trim()) {
    projectKeys = [];
    String(custom).split(',').forEach(function (s) {
      var k = s.trim().toUpperCase();
      if (k) projectKeys.push(k);
    });
  }

  var report = 'Latest Open-Breached counts (from sheets):\n';
  for (var i = 0; i < projectKeys.length; i++) {
    var pk = projectKeys[i];
    var sheet = ss.getSheetByName(pk);
    if (!sheet || sheet.getMaxRows() < 2) continue;
    var data = sheet.getRange(2, 1, 1, 7).getValues()[0];
    var date = data[1];
    var openCount = data[6];
    var dateStr = '';
    if (date instanceof Date && !isNaN(date.getTime())) {
      dateStr = Utilities.formatDate(date, 'UTC', 'yyyy-MM-dd');
    } else {
      dateStr = String(date).trim();
    }
    report += pk + ' (' + dateStr + '): ' + openCount + '\n';
  }
  Logger.log(report);
  try {
    SpreadsheetApp.getUi().alert(report);
  } catch (e) {}
}

function runJiraComplianceDaily_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Run from a container-bound spreadsheet');

  var props = PropertiesService.getScriptProperties();
  var base = cdlGetBaseUrl_(props);
  var headers = cdlAuthHeaders_(props);
  var tz = (props.getProperty('JIRA_COMPLIANCE_TIMEZONE') || 'UTC').trim() || 'UTC';
  var startYmd = (props.getProperty('JIRA_COMPLIANCE_START') || CDL_DEFAULT_START).trim();
  var endYmd = cdlTodayYmd_(tz);

  if (cdlCompareYmd_(startYmd, endYmd) > 0) {
    throw new Error('JIRA_COMPLIANCE_START must be on or before today in ' + tz);
  }

  var projectKeys = cdlGetProjectKeys_(props);
  var baseScope = (props.getProperty('JIRA_COMPLIANCE_BASE_SCOPE') || CDL_BASE_SCOPE_DEFAULT).trim();
  var skipOpen = /^true$/i.test(props.getProperty('JIRA_COMPLIANCE_SKIP_OPEN_PAST_SLA') || '');
  var openSleepMs = parseInt(props.getProperty('JIRA_COMPLIANCE_OPEN_PAST_SLEEP_MS') || '0', 10) || 0;

  var resolvedLtYmd = cdlAddDaysYmd_(endYmd, 1);

  var probePk = cdlProbeProjectKey_(projectKeys);
  var probeJql =
    'project = ' +
    probePk +
    ' AND labels = "vulnerability/sca" AND labels = "vulnerability/fixable" ORDER BY updated DESC';

  var ids = cdlResolveCustomFieldIds_(base, headers, probeJql, props);
  var issueFields = ['key', 'project', 'labels', 'resolutiondate', ids.severityId, ids.introducedId];

  var jql = cdlBuildMainJql_(baseScope, startYmd, resolvedLtYmd, projectKeys);
  var maxResults = 100;

  var allIssues = [];
  var nextToken = null;
  do {
    var body = { jql: jql, maxResults: maxResults, fields: issueFields };
    if (nextToken) body.nextPageToken = nextToken;
    var resp = UrlFetchApp.fetch(base + '/rest/api/3/search/jql', {
      method: 'post',
      contentType: 'application/json',
      headers: headers,
      payload: JSON.stringify(body),
      muteHttpExceptions: true
    });
    if (resp.getResponseCode() < 200 || resp.getResponseCode() >= 300) {
      throw new Error('Jira search failed: ' + resp.getResponseCode() + ' ' + resp.getContentText().substring(0, 600));
    }
    var data = JSON.parse(resp.getContentText());
    var batch = data.issues || [];
    for (var b = 0; b < batch.length; b++) allIssues.push(batch[b]);
    nextToken = data.nextPageToken;
    if (data.isLast || !nextToken) break;
  } while (true);

  var counts = cdlAggregateIssues_(allIssues, tz, ids.severityId, ids.introducedId);
  var dateList = cdlIterDatesInclusive_(startYmd, endYmd);

  var openPastCache = {};

  for (var pi = 0; pi < projectKeys.length; pi++) {
    var pk = projectKeys[pi];
    var outRows = [];
    for (var di = 0; di < dateList.length; di++) {
      var dstr = dateList[di];
      var c = counts[pk + '\t' + dstr] || { fixed: 0, breached: 0 };
      var fixed = c.fixed;
      var breached = c.breached;
      var within = fixed - breached;
      var dailyPct = cdlCompliancePct_(fixed, breached);
      var dailyStr = fixed > 0 && dailyPct != null ? dailyPct + '%' : '';
      outRows.push({
        Project: pk,
        Date: dstr,
        Fixed: fixed,
        Breached: breached,
        Within_SLA: within,
        Daily_Compliance_pct: dailyStr,
        Open_Past_SLA_Count: ''
      });
    }

    if (!skipOpen) {
      cdlPreloadOpenCacheFromSheet_(ss, pk, endYmd, openPastCache);  // restore historical from sheet
      // Pass endYmd so only today is queried live; historical dates are served from the
      // sheet cache above. Passing null would query Jira for every un-cached historical
      // date (up to dateList.length × projectKeys.length API calls — very slow).
      cdlAttachOpenPastSla_(base, headers, baseScope, outRows, maxResults, openSleepMs, openPastCache, endYmd);
    }

    outRows = cdlApplyCumulative_(outRows);
    outRows.reverse();

    cdlWriteOneProjectSheet_(ss, pk, outRows, endYmd, CDL_OPEN_BREACHED_DASHBOARD_URL);
    // Flush after each project so GAS does not batch all 19 projects' setValues /
    // clearContent / setFormula calls into one large flush at the final alert().
    SpreadsheetApp.flush();

    Logger.log(
      'Jira compliance: finished project ' + pk + ' (' + (pi + 1) + '/' + projectKeys.length + ')'
    );
  }

  var summarySheet = ss.getSheetByName(CDL_SUMMARY_SHEET_NAME);
  if (summarySheet) {
    var istTz = 'Asia/Kolkata';
    var istStamp = Utilities.formatDate(new Date(), istTz, 'd MMM, h:mm a') + ' IST';
    summarySheet
      .getRange(CDL_SUMMARY_LAST_UPDATED_ROW, CDL_SUMMARY_LAST_UPDATED_COL)
      .setValue('Last updated: ' + istStamp);
  }

  try {
    SpreadsheetApp.getUi().alert(
      'Jira compliance refresh complete',
      'Projects: ' +
        projectKeys.length +
        '. Issues fetched: ' +
        allIssues.length +
        '. Rows per project: ' +
        dateList.length +
        '. Open past SLA: ' +
        (skipOpen ? 'skipped' : 'computed') +
        '.',
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (e) {
    /* trigger / no UI */
  }
}

/** --- Spreadsheet output --- */

function cdlWriteOneProjectSheet_(ss, projectKey, rows, endYmd, openBreachedUrl) {
  var sheet = cdlEnsureSheet_(ss, projectKey);
  var numCols = CDL_DISPLAY_HEADERS.length;
  var maxRows = sheet.getMaxRows();
  var dataRows = rows.length;

  var dataTable = [];
  for (var j = 0; j < rows.length; j++) {
    dataTable.push(cdlRowToDisplayRow_(rows[j]));
  }
  if (dataRows > 0) {
    // Defensive merge: preserve any existing Open-Breached value (col G) for rows where the new
    // value is empty. This prevents a cold-cache run from zeroing out historical counts.
    var existingRowCount = Math.min(dataRows, Math.max(0, maxRows - 1));
    if (existingRowCount > 0) {
      var existingOpen = sheet.getRange(2, 7, existingRowCount, 1).getValues();
      for (var mi = 0; mi < dataTable.length; mi++) {
        if (mi >= existingOpen.length) break;
        var exv = existingOpen[mi][0];
        var exvNum = typeof exv === 'number' ? exv : parseInt(exv, 10);
        // Only restore a genuinely non-zero existing value; don't propagate the 0-for-all bug.
        if ((dataTable[mi][6] === '' || dataTable[mi][6] == null) &&
            exv !== '' && exv != null && !isNaN(exvNum) && exvNum > 0) {
          dataTable[mi][6] = exvNum;
        }
      }
    }
    sheet.getRange(2, 1, dataRows, numCols).setValues(dataTable);
  }

  cdlFillHeaderRowIfEmpty_(sheet, 1, numCols, CDL_DISPLAY_HEADERS);

  var firstStaleRow = dataRows > 0 ? 2 + dataRows : 2;
  if (firstStaleRow <= maxRows) {
    sheet.getRange(firstStaleRow, 1, maxRows - firstStaleRow + 1, numCols).clearContent();
  }

  if (dataRows > 0 && endYmd && openBreachedUrl) {
    var openCol = 7;
    for (var r = 0; r < rows.length; r++) {
      if (String(rows[r].Date) === String(endYmd)) {
        var raw = rows[r].Open_Past_SLA_Count;
        // Only write HYPERLINK if we have an actual count; don't default empty to 0
        if (raw !== '' && raw !== null && raw !== undefined) {
          var n = parseInt(raw, 10);
          if (!isNaN(n)) {
            sheet.getRange(2 + r, openCol).setFormula(cdlOpenBreachedHyperlinkFormula_(openBreachedUrl, n));
          }
        }
        break;
      }
    }
  }
}

/** HYPERLINK for Open-Breached cell; display is the numeric count (0 allowed). */
function cdlOpenBreachedHyperlinkFormula_(url, displayCount) {
  var u = String(url).replace(/"/g, '""');
  var d = String(displayCount).replace(/"/g, '""');
  return '=HYPERLINK("' + u + '","' + d + '")';
}

/** Batch read/write: one getValues call instead of one getValue per header cell. */
function cdlFillHeaderRowIfEmpty_(sheet, startCol, numCols, headers) {
  var existing = sheet.getRange(1, startCol, 1, numCols).getValues()[0];
  var updated = false;
  for (var c = 0; c < numCols; c++) {
    var v = existing[c];
    if (v === null || v === '' || (typeof v === 'string' && v.trim() === '')) {
      existing[c] = headers[c];
      updated = true;
    }
  }
  if (updated) {
    sheet.getRange(1, startCol, 1, numCols).setValues([existing]);
  }
}

function cdlRowToDisplayRow_(row) {
  return [
    row.Project,
    row.Date,
    row.Fixed,
    row.Breached,
    row.Within_SLA,
    row.Daily_Compliance_pct,
    row.Open_Past_SLA_Count != null ? row.Open_Past_SLA_Count : '',
    row.Cum_Fixed,
    row.Cum_Breached,
    row.Cum_Within_SLA,
    row.Cumulative_Compliance_pct
  ];
}

function cdlEnsureSheet_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (sh) return sh;
  return ss.insertSheet(name);
}

/** --- Jira / logic (aligned with jira_compliance_daily.py) --- */

function cdlGetBaseUrl_(props) {
  var u = props.getProperty('JIRA_BASE_URL');
  if (!u) throw new Error('Set JIRA_BASE_URL in script properties');
  return String(u)
    .trim()
    .replace(/\/+$/, '');
}

function cdlAuthHeaders_(props) {
  var email = props.getProperty('JIRA_EMAIL');
  var token = props.getProperty('JIRA_API_TOKEN');
  if (!email || !token) throw new Error('Set JIRA_EMAIL and JIRA_API_TOKEN');
  var enc = Utilities.base64Encode(email + ':' + token);
  return {
    Authorization: 'Basic ' + enc,
    Accept: 'application/json',
    'Content-Type': 'application/json'
  };
}

function cdlGetProjectKeys_(props) {
  var raw = props.getProperty('JIRA_COMPLIANCE_PROJECT_KEYS');
  if (raw && String(raw).trim()) {
    var keys = [];
    String(raw)
      .split(',')
      .forEach(function (s) {
        var k = s.trim().toUpperCase();
        if (k) keys.push(k);
      });
    if (keys.length) return keys;
  }
  return CDL_MAPPING_PROJECT_KEYS_DEFAULT.slice();
}

function cdlProbeProjectKey_(projectKeys) {
  for (var i = 0; i < projectKeys.length; i++) {
    if (projectKeys[i] === 'OAA') return 'OAA';
  }
  return projectKeys.length ? projectKeys[0] : 'OAA';
}

function cdlBuildMainJql_(baseScope, resolvedGteYmd, resolvedLtYmd, projectKeys) {
  var parts = [
    '(' + baseScope + ')',
    '(labels = "vulnerability/sca" OR labels = "vulnerability/sast")',
    'labels = "vulnerability/fixable"',
    'labels != "vulnerability/ignored"',
    'resolution IS NOT EMPTY'
  ];
  if (projectKeys && projectKeys.length) {
    parts.push('project in (' + projectKeys.slice().sort().join(', ') + ')');
  }
  parts.push('resolved >= "' + resolvedGteYmd + '"');
  parts.push('resolved < "' + resolvedLtYmd + '"');
  return parts.join(' AND ');
}

function cdlResolveCustomFieldIds_(base, headers, probeJql, props) {
  var envSev = (props.getProperty('JIRA_FIELD_SEVERITY') || '').trim();
  var envIntro = (props.getProperty('JIRA_FIELD_VULNERABILITY_INTRODUCED') || '').trim();

  var resp = UrlFetchApp.fetch(base + '/rest/api/3/field', {
    method: 'get',
    headers: headers,
    muteHttpExceptions: true
  });
  if (resp.getResponseCode() !== 200) throw new Error('GET /field failed: ' + resp.getContentText().substring(0, 300));
  var fields = JSON.parse(resp.getContentText());
  var byName = {};
  for (var i = 0; i < fields.length; i++) {
    if (fields[i].name) byName[fields[i].name] = fields[i].id;
  }

  var introducedId = envIntro || null;
  if (!introducedId) {
    for (var j = 0; j < CDL_INTRODUCED_NAMES.length; j++) {
      if (byName[CDL_INTRODUCED_NAMES[j]]) {
        introducedId = byName[CDL_INTRODUCED_NAMES[j]];
        break;
      }
    }
  }
  if (!introducedId) throw new Error('Set JIRA_FIELD_VULNERABILITY_INTRODUCED or fix introduced field lookup');

  var severityId = envSev || null;
  if (!severityId) {
    var candidates = [];
    for (var k = 0; k < fields.length; k++) {
      var fld = fields[k];
      if (fld.name === 'Severity' && fld.schema && fld.schema.type === 'option') {
        candidates.push(fld.id);
      }
    }
    if (candidates.length === 1) severityId = candidates[0];
    else if (candidates.length > 1) {
      severityId = cdlProbeSeverityFieldId_(base, headers, probeJql, candidates);
      if (!severityId) throw new Error('Multiple Severity fields; set JIRA_FIELD_SEVERITY');
    } else throw new Error('No Severity option field found');
  }

  return { severityId: severityId, introducedId: introducedId };
}

function cdlProbeSeverityFieldId_(base, headers, jql, candidateIds) {
  var body = { jql: jql, maxResults: 25, fields: candidateIds };
  var resp = UrlFetchApp.fetch(base + '/rest/api/3/search/jql', {
    method: 'post',
    contentType: 'application/json',
    headers: headers,
    payload: JSON.stringify(body),
    muteHttpExceptions: true
  });
  if (resp.getResponseCode() !== 200) return null;
  var data = JSON.parse(resp.getContentText());
  var issues = data.issues || [];
  for (var i = 0; i < issues.length; i++) {
    var flds = issues[i].fields || {};
    for (var c = 0; c < candidateIds.length; c++) {
      var sid = candidateIds[c];
      var v = cdlParseSeverityValue_(flds[sid]);
      if (v && CDL_SEVERITY_SLA_DAYS[v] != null) return sid;
    }
  }
  return null;
}

function cdlParseSeverityValue_(raw) {
  if (raw == null) return null;
  if (typeof raw === 'string') return raw.trim() || null;
  if (typeof raw === 'object' && raw.value) return String(raw.value).trim() || null;
  return null;
}

function cdlParseIntroducedDate_(raw) {
  if (raw == null) return null;
  // Jira sometimes returns date custom fields as an object {value: "YYYY-MM-DD", ...}
  // String(raw) on an object yields "[object Object]" — extract the value first.
  if (typeof raw === 'object' && !Array.isArray(raw)) {
    raw = raw.value || raw.date || raw.iso8601 || null;
    if (!raw) return null;
  }
  var s = String(raw).trim();
  if (!s) return null;
  if (s.indexOf('T') >= 0) {
    var d = new Date(s.replace(/Z$/, '+00:00'));
    if (isNaN(d.getTime())) return null;
    return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  }
  var parts = s.substring(0, 10).split('-');
  if (parts.length !== 3) return null;
  return new Date(Date.UTC(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10)));
}

function cdlParseResolutionUtc_(raw) {
  if (raw == null) return null;
  var s = String(raw).trim();
  if (!s) return null;
  var d = new Date(s.replace(/Z$/, '+00:00'));
  if (isNaN(d.getTime())) return null;
  return d;
}

function cdlLocalDateString_(d, tz) {
  return Utilities.formatDate(d, tz, 'yyyy-MM-dd');
}

function cdlProjectKeyFromIssue_(issue) {
  var proj = (issue.fields || {}).project;
  if (!proj || typeof proj !== 'object') return null;
  return proj.key ? String(proj.key) : null;
}

function cdlAggregateIssues_(issues, tz, severityId, introducedId) {
  var counts = {};
  for (var i = 0; i < issues.length; i++) {
    var issue = issues[i];
    var pk = cdlProjectKeyFromIssue_(issue);
    if (!pk) continue;
    var fields = issue.fields || {};
    var labels = fields.labels || [];
    if (!Array.isArray(labels)) labels = [];
    var resUtc = cdlParseResolutionUtc_(fields.resolutiondate);
    if (resUtc == null) continue;
    var dstr = cdlLocalDateString_(resUtc, tz);
    var key = pk + '\t' + dstr;
    if (!counts[key]) counts[key] = { fixed: 0, breached: 0 };
    counts[key].fixed += 1;

    var sev = cdlParseSeverityValue_(fields[severityId]);
    var intro = cdlParseIntroducedDate_(fields[introducedId]);
    var resDate = cdlParseResolutionDateForSla_(fields.resolutiondate);
    var breachedInc = false;
    if (sev && intro && resDate && CDL_SEVERITY_SLA_DAYS[sev] != null) {
      var br = cdlSlaBreached_(intro, sev, resDate);
      breachedInc = br.breached;
    } else if (labels.indexOf(CDL_SLA_LABEL) >= 0) {
      breachedInc = true;
    }
    if (breachedInc) counts[key].breached += 1;
  }
  return counts;
}

function cdlParseResolutionDateForSla_(raw) {
  if (raw == null) return null;
  var s = String(raw).trim();
  if (!s) return null;
  var d = new Date(s.replace(/Z$/, '+00:00'));
  if (isNaN(d.getTime())) return null;
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

function cdlSlaBreached_(introduced, sev, resolved) {
  var n = CDL_SEVERITY_SLA_DAYS[sev];
  if (n == null) return { breached: false, n: null };
  var due = new Date(introduced.getTime());
  due.setUTCDate(due.getUTCDate() + n);
  return { breached: resolved.getTime() > due.getTime(), n: n };
}

function cdlCompliancePct_(fixed, breached) {
  if (fixed <= 0) return null;
  return Math.round(10000 * (1 - breached / fixed)) / 100;
}

function cdlApplyCumulative_(rows) {
  var sorted = rows.slice().sort(function (a, b) {
    if (a.Project !== b.Project) return String(a.Project).localeCompare(String(b.Project));
    return String(a.Date).localeCompare(String(b.Date));
  });
  var out = [];
  var cumF = 0;
  var cumB = 0;
  var curProj = null;
  for (var i = 0; i < sorted.length; i++) {
    var r = sorted[i];
    var pk = String(r.Project);
    if (pk !== curProj) {
      cumF = 0;
      cumB = 0;
      curProj = pk;
    }
    var fixed = parseInt(r.Fixed, 10) || 0;
    var breached = parseInt(r.Breached, 10) || 0;
    cumF += fixed;
    cumB += breached;
    var cumW = cumF - cumB;
    var cc = cdlCompliancePct_(cumF, cumB);
    var row = {};
    for (var k in r) {
      if (r.hasOwnProperty(k)) row[k] = r[k];
    }
    row.Cum_Fixed = cumF;
    row.Cum_Breached = cumB;
    row.Cum_Within_SLA = cumW;
    row.Cumulative_Compliance_pct = cumF > 0 && cc != null ? cc + '%' : '';
    out.push(row);
  }
  return out;
}

function cdlBuildOpenPastSlaJql_(baseScope, projectKey, snapshotYmd, doneStatuses) {
  var sevParts = [];
  var keys = Object.keys(CDL_SEVERITY_SLA_DAYS);
  for (var k = 0; k < keys.length; k++) {
    var sev = keys[k];
    var n = CDL_SEVERITY_SLA_DAYS[sev];
    sevParts.push(
      '("Severity[Dropdown]" = ' +
        sev +
        ' AND "vulnerability_introduced_date[Date]" < -' + n + 'd)'
    );
  }
  var inner = doneStatuses
    .map(function (s) {
      return s.trim();
    })
    .filter(function (s) {
      return s;
    })
    .join(', ');
  var statusNotIn = 'status NOT IN (' + inner + ')';
  var parts = [
    '(' + baseScope + ')',
    '(labels = "vulnerability/sca" OR labels = "vulnerability/sast")',
    'labels = "vulnerability/fixable"',
    'labels != "vulnerability/ignored"',
    'project = ' + projectKey,
    statusNotIn,
    '(' + sevParts.join(' OR ') + ')'
  ];
  return parts.join(' AND ');
}

function cdlCountJqlIssues_(base, headers, jql, maxResults) {
  var count = 0;
  var nextToken = null;
  do {
    var body = { jql: jql, maxResults: 100, fields: ['key'] };
    if (nextToken) body.nextPageToken = nextToken;
    var resp = UrlFetchApp.fetch(base + '/rest/api/3/search/jql', {
      method: 'post',
      contentType: 'application/json',
      headers: headers,
      payload: JSON.stringify(body),
      muteHttpExceptions: true
    });
    if (resp.getResponseCode() < 200 || resp.getResponseCode() >= 300) {
      throw new Error('Count JQL failed: ' + resp.getResponseCode() + ' ' + resp.getContentText().substring(0, 400));
    }
    var data = JSON.parse(resp.getContentText());
    count += (data.issues || []).length;
    nextToken = data.nextPageToken;
    if (data.isLast || !nextToken) break;
  } while (true);
  Logger.log('cdlCountJqlIssues_ JQL: ' + jql + ' → count=' + count);
  return count;
}

/**
 * @param {Object=} cacheOpt - optional shared cache (pk+date → count) across projects
 * @param {string=} onlyYmd  - when set, only fetches open-past-SLA for this date (today).
 *   Historical rows are left empty — they were written by previous daily runs and the
 *   HYPERLINK formula only ever uses today's count anyway. Reduces API calls from
 *   (projects × days) down to (projects × 1).
 */
function cdlAttachOpenPastSla_(base, headers, baseScope, rows, maxResults, sleepMs, cacheOpt, onlyYmd) {
  var cache = cacheOpt || {};
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var pk = String(row.Project);
    var dstr = String(row.Date);
    var ck = pk + '\t' + dstr;
    // Cache check FIRST: historical values preloaded from the sheet satisfy this.
    if (cache.hasOwnProperty(ck)) {
      row.Open_Past_SLA_Count = cache[ck];
      continue;
    }
    // Skip the live Jira fetch for any date other than today.
    if (onlyYmd && dstr !== onlyYmd) continue;
    var jql = cdlBuildOpenPastSlaJql_(baseScope, pk, dstr, CDL_OPEN_DONE_STATUSES);
    var n = cdlCountJqlIssues_(base, headers, jql, maxResults);
    cache[ck] = n;
    row.Open_Past_SLA_Count = n;
    if (sleepMs > 0) Utilities.sleep(sleepMs);
  }
}

/**
 * Reads existing Open-Breached values from the project sheet into the shared cache so that
 * cdlAttachOpenPastSla_ can serve historical dates without re-querying Jira.
 * One batch getValues call per project; skips today (which will be fetched fresh).
 */
function cdlPreloadOpenCacheFromSheet_(ss, projectKey, endYmd, cache) {
  var sheet = ss.getSheetByName(projectKey);
  if (!sheet) return;
  var maxRows = sheet.getMaxRows();
  if (maxRows < 2) return;
  var sheetTz = ss.getSpreadsheetTimeZone();
  // Batch read cols A–G (1–7): date is col B (idx 1), Open-Breached is col G (idx 6).
  var data = sheet.getRange(2, 1, maxRows - 1, 7).getValues();
  for (var i = 0; i < data.length; i++) {
    var eDate = data[i][1];
    var eOpen = data[i][6];
    var eDateStr = '';
    if (eDate instanceof Date && !isNaN(eDate.getTime())) {
      eDateStr = Utilities.formatDate(eDate, sheetTz, 'yyyy-MM-dd');
    } else if (eDate !== null && eDate !== undefined && eDate !== '') {
      eDateStr = String(eDate).trim();
    }
    if (!eDateStr || eDateStr === endYmd) continue; // today fetched fresh
    var ck = projectKey + '\t' + eDateStr;
    if (cache.hasOwnProperty(ck)) continue;
    if (eOpen === '' || eOpen === null || eOpen === undefined) continue;
    var n = parseInt(eOpen, 10);
    if (!isNaN(n)) cache[ck] = n;
  }
}

/** --- Date helpers --- */

function cdlTodayYmd_(tz) {
  return Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
}

function cdlIterDatesInclusive_(startYmd, endYmd) {
  var out = [];
  var cur = new Date(startYmd + 'T12:00:00Z');
  var end = new Date(endYmd + 'T12:00:00Z');
  while (cur.getTime() <= end.getTime()) {
    out.push(Utilities.formatDate(cur, 'UTC', 'yyyy-MM-dd'));
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
  return out;
}

function cdlAddDaysYmd_(ymd, deltaDays) {
  var p = ymd.split('-');
  var d = new Date(Date.UTC(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10)));
  d.setUTCDate(d.getUTCDate() + deltaDays);
  var y = d.getUTCFullYear();
  var m = ('0' + (d.getUTCMonth() + 1)).slice(-2);
  var day = ('0' + d.getUTCDate()).slice(-2);
  return y + '-' + m + '-' + day;
}

function cdlCompareYmd_(a, b) {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

/**
 * One-time backfill: for every project tab, fills empty Open-Breached (col G) cells for
 * historical dates by fetching all relevant issues once per project and computing point-in-time
 * open-past-SLA counts in memory (no per-date Jira queries).
 *
 * Run manually via "Jira compliance → Backfill historical Open-Breached (one-time)".
 * Progress is saved in script properties (CDL_BACKFILL_COMPLETED) so a GAS-timeout mid-run
 * can be resumed by re-running the function.
 */
function runBackfillOpenBreached_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Run from a container-bound spreadsheet');

  var props = PropertiesService.getScriptProperties();
  var base = cdlGetBaseUrl_(props);
  var authHdrs = cdlAuthHeaders_(props);
  var tz = (props.getProperty('JIRA_COMPLIANCE_TIMEZONE') || 'UTC').trim() || 'UTC';
  var startYmd = (props.getProperty('JIRA_COMPLIANCE_START') || CDL_DEFAULT_START).trim();
  var endYmd = cdlTodayYmd_(tz);
  var projectKeys = cdlGetProjectKeys_(props);
  var baseScope = (props.getProperty('JIRA_COMPLIANCE_BASE_SCOPE') || CDL_BASE_SCOPE_DEFAULT).trim();
  var sheetTz = ss.getSpreadsheetTimeZone();

  var probePk = cdlProbeProjectKey_(projectKeys);
  var probeJql = 'project = ' + probePk + ' AND labels = "vulnerability/sca" AND labels = "vulnerability/fixable" ORDER BY updated DESC';
  var ids = cdlResolveCustomFieldIds_(base, authHdrs, probeJql, props);
  var issueFields = ['key', 'created', 'project', 'resolutiondate', ids.severityId, ids.introducedId];

  var completedStr = props.getProperty('CDL_BACKFILL_COMPLETED') || '';
  var completed = completedStr ? completedStr.split(',').filter(function(s) { return s; }) : [];

  var totalFilled = 0;

  for (var pi = 0; pi < projectKeys.length; pi++) {
    var pk = projectKeys[pi];
    if (completed.indexOf(pk) >= 0) {
      Logger.log('SNYK backfill: skipping ' + pk + ' (already done)');
      continue;
    }

    var sheet = ss.getSheetByName(pk);
    if (!sheet) {
      Logger.log('SNYK backfill: no sheet for ' + pk);
      completed.push(pk);
      props.setProperty('CDL_BACKFILL_COMPLETED', completed.join(','));
      continue;
    }

    var maxRows = sheet.getMaxRows();
    if (maxRows < 2) {
      completed.push(pk);
      props.setProperty('CDL_BACKFILL_COMPLETED', completed.join(','));
      continue;
    }

    // Read cols A–G to locate rows with empty Open-Breached (col G = index 6).
    var sheetData = sheet.getRange(2, 1, maxRows - 1, 7).getValues();
    var needBackfill = {}; // dateStr → 0-based row index in sheetData
    for (var ri = 0; ri < sheetData.length; ri++) {
      var eDate = sheetData[ri][1];
      var eOpen = sheetData[ri][6];
      // Skip only rows that already have a confirmed non-zero value.
      // Treat 0 as "needs backfill" — the optimization set 0 for all historical rows.
      var hasRealValue = (eOpen !== '' && eOpen !== null && eOpen !== undefined && eOpen !== 0 && eOpen !== '0');
      if (hasRealValue) continue;
      var eDateStr = '';
      if (eDate instanceof Date && !isNaN(eDate.getTime())) {
        eDateStr = Utilities.formatDate(eDate, sheetTz, 'yyyy-MM-dd');
      } else if (eDate !== null && eDate !== undefined && eDate !== '') {
        eDateStr = String(eDate).trim();
      }
      if (!eDateStr || eDateStr === endYmd) continue; // skip today (fetched live by daily run)
      needBackfill[eDateStr] = ri;
    }

    var needDates = Object.keys(needBackfill);
    if (needDates.length === 0) {
      Logger.log('SNYK backfill: ' + pk + ' — no rows need backfill, skipping');
      completed.push(pk);
      props.setProperty('CDL_BACKFILL_COMPLETED', completed.join(','));
      continue;
    }

    Logger.log('SNYK backfill: ' + pk + ' — ' + needDates.length + ' dates need fill; fetching issues…');

    // One broad Jira query per project: issues open at any point during the tracked period.
    var backfillJql = [
      '(' + baseScope + ')',
      '(labels = "vulnerability/sca" OR labels = "vulnerability/sast")',
      'labels = "vulnerability/fixable"',
      'labels != "vulnerability/ignored"',
      'project = ' + pk,
      '(resolved >= "' + startYmd + '" OR resolved IS EMPTY)'
    ].join(' AND ');

    var issues = [];
    var nextToken = null;
    do {
      var body = { jql: backfillJql, maxResults: 100, fields: issueFields };
      if (nextToken) body.nextPageToken = nextToken;
      var resp = UrlFetchApp.fetch(base + '/rest/api/3/search/jql', {
        method: 'post',
        contentType: 'application/json',
        headers: authHdrs,
        payload: JSON.stringify(body),
        muteHttpExceptions: true
      });
      if (resp.getResponseCode() < 200 || resp.getResponseCode() >= 300) {
        throw new Error('SNYK backfill search failed for ' + pk + ': ' + resp.getResponseCode() + ' ' + resp.getContentText().substring(0, 400));
      }
      var pageData = JSON.parse(resp.getContentText());
      var batch = pageData.issues || [];
      for (var b = 0; b < batch.length; b++) issues.push(batch[b]);
      nextToken = pageData.nextPageToken;
      if (pageData.isLast || !nextToken) break;
    } while (true);

    Logger.log('SNYK backfill: ' + pk + ' — ' + issues.length + ' issues fetched');

    // Parse each issue into minimal structs for O(issues×dates) in-memory computation.
    var issueData = [];
    for (var ii = 0; ii < issues.length; ii++) {
      var flds = issues[ii].fields || {};
      var createdFull = cdlParseResolutionUtc_(flds.created);
      if (!createdFull) continue;
      var createdDayMs = Date.UTC(createdFull.getUTCFullYear(), createdFull.getUTCMonth(), createdFull.getUTCDate());
      var resolvedFull = cdlParseResolutionUtc_(flds.resolutiondate);
      var resolvedDayMs = resolvedFull
        ? Date.UTC(resolvedFull.getUTCFullYear(), resolvedFull.getUTCMonth(), resolvedFull.getUTCDate())
        : null;
      var sev = cdlParseSeverityValue_(flds[ids.severityId]);
      var intro = cdlParseIntroducedDate_(flds[ids.introducedId]);
      if (!sev || !intro || CDL_SEVERITY_SLA_DAYS[sev] == null) continue;
      var slaDeadlineMs = intro.getTime() + CDL_SEVERITY_SLA_DAYS[sev] * 86400000;
      issueData.push({ createdDayMs: createdDayMs, resolvedDayMs: resolvedDayMs, slaDeadlineMs: slaDeadlineMs });
    }

    Logger.log('SNYK backfill: ' + pk + ' — ' + issueData.length + ' issues with full SLA data');

    // For each date needing backfill, count issues that were open AND past SLA on that date.
    for (var di = 0; di < needDates.length; di++) {
      var dstr = needDates[di];
      var dp = dstr.split('-');
      var dMs = Date.UTC(parseInt(dp[0], 10), parseInt(dp[1], 10) - 1, parseInt(dp[2], 10));
      var count = 0;
      for (var ij = 0; ij < issueData.length; ij++) {
        var iss = issueData[ij];
        // Open on date D: created on or before D AND not yet resolved on D
        if (iss.createdDayMs > dMs) continue;
        if (iss.resolvedDayMs !== null && iss.resolvedDayMs <= dMs) continue;
        // Past SLA on D: SLA deadline fell before midnight of D
        if (iss.slaDeadlineMs < dMs) count++;
      }
      sheet.getRange(2 + needBackfill[dstr], 7).setValue(count);
      totalFilled++;
    }

    SpreadsheetApp.flush();
    Logger.log('SNYK backfill: ' + pk + ' done — wrote ' + needDates.length + ' cells (' + (pi + 1) + '/' + projectKeys.length + ')');
    completed.push(pk);
    props.setProperty('CDL_BACKFILL_COMPLETED', completed.join(','));
  }

  props.deleteProperty('CDL_BACKFILL_COMPLETED');

  try {
    SpreadsheetApp.getUi().alert(
      'SNYK Open-Breached backfill complete',
      'Filled ' + totalFilled + ' historical Open-Breached cells across ' + projectKeys.length + ' projects.\n\n' +
      'Run "Refresh report" to confirm the chart trend is restored.',
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (e) {
    Logger.log('SNYK backfill done: ' + totalFilled + ' cells filled');
  }
}
