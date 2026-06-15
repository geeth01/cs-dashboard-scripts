/**
 * Jira daily SLA compliance — VAPT label scope (separate run from JiraComplianceDaily.gs).
 *
 * Same math and layout as SNYK/SCA report, but issues are scoped with:
 *   labels in (internal_vapt_vulnerability, external_vapt_vulnerability)
 * instead of (vulnerability/sca OR vulnerability/sast) + fixable + not ignored.
 *
 * Writes to the SAME project tabs as the SNYK script:
 *   - Project sheets: table starting at column M (M1 = headers) — columns A–L unchanged (SNYK block).
 *   The Summary sheet is not otherwise modified; on successful completion only cell D25 is set to
 *   "Last updated: … IST" (Asia/Kolkata) if a tab named "Summary" exists (same as JiraComplianceDaily.gs).
 *
 * Run this in a separate execution from JiraComplianceDaily.gs so total wall time splits across runs.
 *
 * SETUP: same script properties as JiraComplianceDaily (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, …).
 * Optional: JIRA_COMPLIANCE_VAPT_SKIP_OPEN_PAST_SLA — if set, overrides JIRA_COMPLIANCE_SKIP_OPEN_PAST_SLA for this script only;
 *   when open-past-SLA is skipped, today's Open-Breached cell still shows HYPERLINK with 0.
 *
 * Merge menu: cdvOnOpen_() in your onOpen next to cdlOnOpen_().
 *
 * Project sheets: values from row 2 in columns M onward; M1:W1 filled only where empty; data rows are
 * newest-first (today in row 2); stale rows below last data cleared. Today's Open-Breached (VAPT block)
 * is a HYPERLINK to the shared Jira dashboard.
 *
 * After M–W is written, columns Y–AH hold Combined SNYK+VAPT: sum C,D,E,G,H,I,J from A–K and M–W (omit F,R
 * daily %; omit K,W cum % — last column recomputed). Requires column A–K from a recent JiraComplianceDaily
 * run (matching date span and row order); empty SNYK cells are treated as zero.
 */

var CDV_SLA_LABEL = 'sla-breached';

var CDV_SEVERITY_SLA_DAYS = {
  'Sev-0': 14,
  'Sev-1': 30,
  'Sev-2': 90,
  'Sev-3': 180
};

/** Stage-2 SLA (days): issue mitigated but not yet closed. */
var CDV_SEVERITY_SLA_STAGE2_DAYS = {
  'Sev-0': 30,
  'Sev-1': 180,
  'Sev-2': 365,
  'Sev-3': 365
};

var CDV_INTRODUCED_NAMES = ['vulnerability_introduced_date', 'vulnerability_introduced_date[Date]'];

var CDV_BASE_SCOPE_DEFAULT =
  'project in (AH, AM, DAT, OAA, CMA, CD, RS, ECL, CL, MKT, CSI, DX, AO, SS, VB, VP, COMS, GROW, EXP)';

var CDV_MAPPING_PROJECT_KEYS_DEFAULT = [
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
  'EXP',
];

/** Same columns as report.csv / SNYK block; written starting at column M. */
var CDV_DISPLAY_HEADERS = [
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

/** Column M (1-based). A–L reserved for SNYK compliance table. */
var CDV_DATA_START_COL = 13;

/** Column Y (1-based). Combined SNYK+VAPT metrics after each project write. */
var CDV_COMBINED_START_COL = 25;

/** Full combined mirror: daily + open + cumulative sums + Combined-Cumulative-Compliance. */
var CDV_COMBINED_HEADERS = [
  'Project',
  'Date',
  'Fixed-Total',
  'Fixed-Breached',
  'Fixed-Within_SLA',
  'Open-Breached',
  'Cumulative-Fixed-Total',
  'Cumulative-Fixed-Breached',
  'Cumulative-Fixed-Within_SLA',
  'Cumulative-Compliance'
];

var CDV_SUMMARY_SHEET_NAME = 'Summary';
var CDV_SUMMARY_LAST_UPDATED_ROW = 25;
var CDV_SUMMARY_LAST_UPDATED_COL = 4;
var CDV_DEFAULT_START = '2026-02-01';
// VAPT open-past-SLA: Rejected is intentionally NOT excluded (matches actual Jira query).
var CDV_OPEN_DONE_STATUSES = ['Done', 'Archived'];

/** Open-Breached (today's row, VAPT block) links here for every project tab. */
var CDV_OPEN_BREACHED_DASHBOARD_URL = 'https://contentstack.atlassian.net/jira/dashboards/11219';

/** JQL fragment for VAPT issues (replaces SCA/SAST + fixable + not ignored). */
var CDV_LABEL_SCOPE_JQL = 'labels in ("internal_vapt_vulnerability", "external_vapt_vulnerability")';

function cdvOnOpen_() {
  SpreadsheetApp.getUi()
    .createMenu('Jira compliance VAPT')
    .addItem('Refresh VAPT report (Jira → column M & Summary C)', 'runJiraComplianceDailyVapt_')
    .addItem('Backfill historical Open-Breached VAPT (one-time)', 'runBackfillOpenBreachedVapt_')
    .addToUi();
}

function runJiraComplianceDailyVapt() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Run from a container-bound spreadsheet');

  var props = PropertiesService.getScriptProperties();
  var base = cdvGetBaseUrl_(props);
  var headers = cdvAuthHeaders_(props);
  var tz = (props.getProperty('JIRA_COMPLIANCE_TIMEZONE') || 'UTC').trim() || 'UTC';
  var startYmd = (props.getProperty('JIRA_COMPLIANCE_START') || CDV_DEFAULT_START).trim();
  var endYmd = cdvTodayYmd_(tz);

  if (cdvCompareYmd_(startYmd, endYmd) > 0) {
    throw new Error('JIRA_COMPLIANCE_START must be on or before today in ' + tz);
  }

  var projectKeys = cdvGetProjectKeys_(props);
  var baseScope = (props.getProperty('JIRA_COMPLIANCE_BASE_SCOPE') || CDV_BASE_SCOPE_DEFAULT).trim();
  var skipOpen = cdvGetSkipOpenPastSla_(props);
  var openSleepMs = parseInt(props.getProperty('JIRA_COMPLIANCE_OPEN_PAST_SLEEP_MS') || '0', 10) || 0;

  Logger.log('VAPT run — projects: ' + projectKeys.join(', '));
  Logger.log('VAPT run — skipOpen=' + skipOpen + '  (set JIRA_COMPLIANCE_SKIP_OPEN_PAST_SLA=false to compute real counts)');
  // Sample the open-past-SLA JQL for the first project so it is visible in execution logs.
  Logger.log('VAPT sample open-past-SLA JQL: ' + cdvBuildOpenPastSlaJql_(baseScope, projectKeys[0], cdvTodayYmd_(tz), CDV_OPEN_DONE_STATUSES));

  var resolvedLtYmd = cdvAddDaysYmd_(endYmd, 1);

  var probePk = cdvProbeProjectKey_(projectKeys);
  var probeJql =
    'project = ' +
    probePk +
    ' AND ' +
    CDV_LABEL_SCOPE_JQL +
    ' ORDER BY updated DESC';

  var ids = cdvResolveCustomFieldIds_(base, headers, probeJql, props);
  var issueFields = ['key', 'project', 'labels', 'resolutiondate', ids.severityId, ids.introducedId];

  var jql = cdvBuildMainJql_(baseScope, startYmd, resolvedLtYmd, projectKeys);
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

  var counts = cdvAggregateIssues_(allIssues, tz, ids.severityId, ids.introducedId);
  var dateList = cdvIterDatesInclusive_(startYmd, endYmd);

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
      var dailyPct = cdvCompliancePct_(fixed, breached);
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
      cdvPreloadOpenCacheFromSheet_(ss, pk, endYmd, openPastCache);  // restore historical from sheet
      // Pass endYmd so only today is queried live; historical dates are served from the
      // sheet cache above. Passing null would query Jira for every un-cached historical
      // date (up to dateList.length × projectKeys.length API calls — very slow).
      cdvAttachOpenPastSla_(base, headers, baseScope, outRows, maxResults, openSleepMs, openPastCache, endYmd);
    }

    outRows = cdvApplyCumulative_(outRows);
    outRows.reverse();

    cdvWriteOneProjectSheet_(ss, pk, outRows, endYmd, CDV_OPEN_BREACHED_DASHBOARD_URL);
    // Flush after each project so GAS does not batch all 19 projects' setValues /
    // clearContent / setFormula calls into one large flush at the final alert().
    SpreadsheetApp.flush();

    Logger.log(
      'Jira compliance VAPT: finished project ' + pk + ' (' + (pi + 1) + '/' + projectKeys.length + ')'
    );
  }

  var summarySheet = ss.getSheetByName(CDV_SUMMARY_SHEET_NAME);
  if (summarySheet) {
    var istTz = 'Asia/Kolkata';
    var istStamp = Utilities.formatDate(new Date(), istTz, 'd MMM, h:mm a') + ' IST';
    summarySheet
      .getRange(CDV_SUMMARY_LAST_UPDATED_ROW, CDV_SUMMARY_LAST_UPDATED_COL)
      .setValue('Last updated: ' + istStamp);
  }

  try {
    SpreadsheetApp.getUi().alert(
      'Jira compliance VAPT refresh complete',
      'Projects: ' +
        projectKeys.length +
        '. Issues fetched: ' +
        allIssues.length +
        '. Rows per project: ' +
        dateList.length +
        '. Open past SLA: ' +
        (skipOpen ? 'skipped' : 'computed') +
        '. Data is in column M onward; combined SNYK+VAPT in column Y onward (requires fresh JiraComplianceDaily).',
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (e) {
    /* trigger / no UI */
  }
}

function cdvGetSkipOpenPastSla_(props) {
  var v = props.getProperty('JIRA_COMPLIANCE_VAPT_SKIP_OPEN_PAST_SLA');
  if (v != null && String(v).trim() !== '') {
    return /^true$/i.test(String(v).trim());
  }
  return /^true$/i.test(props.getProperty('JIRA_COMPLIANCE_SKIP_OPEN_PAST_SLA') || '');
}

function cdvWriteOneProjectSheet_(ss, projectKey, rows, endYmd, openBreachedUrl) {
  var sheet = cdvEnsureSheet_(ss, projectKey);
  var startCol = CDV_DATA_START_COL;
  var numCols = CDV_DISPLAY_HEADERS.length;
  var maxRows = sheet.getMaxRows();
  var dataRows = rows.length;

  var dataTable = [];
  for (var j = 0; j < rows.length; j++) {
    dataTable.push(cdvRowToDisplayRow_(rows[j]));
  }
  if (dataRows > 0) {
    // Defensive merge: preserve any existing Open-Breached value (col S) for rows where the new
    // value is empty. This prevents a cold-cache run from zeroing out historical counts.
    var existingRowCount = Math.min(dataRows, Math.max(0, maxRows - 1));
    if (existingRowCount > 0) {
      var existingOpen = sheet.getRange(2, startCol + 6, existingRowCount, 1).getValues();
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
    sheet.getRange(2, startCol, dataRows, numCols).setValues(dataTable);
  }

  cdvFillHeaderRowIfEmpty_(sheet, startCol, numCols, CDV_DISPLAY_HEADERS);

  var firstStaleRow = dataRows > 0 ? 2 + dataRows : 2;
  if (firstStaleRow <= maxRows) {
    sheet.getRange(firstStaleRow, startCol, maxRows - firstStaleRow + 1, numCols).clearContent();
  }

  if (dataRows > 0 && endYmd && openBreachedUrl) {
    var openCol = startCol + 6;
    for (var r = 0; r < rows.length; r++) {
      if (String(rows[r].Date) === String(endYmd)) {
        var raw = rows[r].Open_Past_SLA_Count;
        // Only write HYPERLINK if we have an actual count; don't default empty to 0
        if (raw !== '' && raw !== null && raw !== undefined) {
          var n = parseInt(raw, 10);
          if (!isNaN(n)) {
            sheet.getRange(2 + r, openCol).setFormula(cdvOpenBreachedHyperlinkFormula_(openBreachedUrl, n));
          }
        }
        break;
      }
    }
  }

  cdvWriteCombinedSnykVaptBlock_(sheet, projectKey, dataRows, maxRows);
}

function cdvParseNumericCell_(value) {
  if (value === null || value === undefined) return 0;
  if (typeof value === 'number' && !isNaN(value)) return Math.round(value);
  var s = String(value).trim();
  if (s === '') return 0;
  var n = parseFloat(s.replace(/%/g, '').trim());
  if (isNaN(n)) return 0;
  return Math.round(n);
}

/**
 * Match B/N date to yyyy-MM-dd when reading combined grid via getValues().
 * Date cells must use the spreadsheet timezone — UTC formatting shifts calendar day vs what the sheet displays.
 */
function cdvCellValueToYmd_(value, sheetTz) {
  var tz = sheetTz && String(sheetTz).trim() ? sheetTz : 'UTC';
  if (value === null || value === undefined || value === '') return '';
  if (Object.prototype.toString.call(value) === '[object Date]') {
    if (isNaN(value.getTime())) return '';
    return Utilities.formatDate(value, tz, 'yyyy-MM-dd');
  }
  var s = String(value).trim();
  if (s === '') return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  var d = new Date(s);
  if (!isNaN(d.getTime())) return Utilities.formatDate(d, tz, 'yyyy-MM-dd');
  return s;
}

/**
 * Reads one row from SNYK block (A–K) + VAPT block (M–W) and writes combined metrics to Y–AH.
 *
 * Both blocks use the same 11 headers as CDL_DISPLAY_HEADERS / CDV_DISPLAY_HEADERS (row 1).
 * 0-based indices in getValues() row arrays:
 *   SNYK A..K:  Project=0, Date=1, Fixed=2, Breached=3, Within=4, Daily%=5, Open=6,
 *               CumFixed=7, CumBreached=8, CumWithin=9, CumCompliance%=10
 *   VAPT M..W:  +12 → M=12 .. W=22 (same header order)
 *
 * Combined (= Y..AH): sum numeric columns only; skip Daily-Compliance (F=5, R=17) and skip
 * rolling % columns K & W (10 & 22); last column is Combined-Cumulative-Compliance from
 * cdvCompliancePct_(CumFixed_sum, CumBreached_sum).
 */
function cdvWriteCombinedSnykVaptBlock_(sheet, projectKey, dataRows, maxRows) {
  var startCol = CDV_COMBINED_START_COL;
  var numCombined = CDV_COMBINED_HEADERS.length;
  var firstStaleRow = dataRows > 0 ? 2 + dataRows : 2;

  if (firstStaleRow <= maxRows) {
    sheet.getRange(firstStaleRow, startCol, maxRows - firstStaleRow + 1, numCombined).clearContent();
  }

  if (dataRows <= 0) {
    cdvFillHeaderRowIfEmpty_(sheet, startCol, numCombined, CDV_COMBINED_HEADERS);
    return;
  }

  var grid = sheet.getRange(2, 1, dataRows, 23).getValues();
  var combinedTable = [];
  var pkExpect = String(projectKey).trim();
  var sheetTz = sheet.getParent().getSpreadsheetTimeZone();

  for (var r = 0; r < grid.length; r++) {
    var row = grid[r];
    var snykProj = row[0] != null ? String(row[0]).trim() : '';
    var vaptProj = row[12] != null ? String(row[12]).trim() : '';
    var snykDate = cdvCellValueToYmd_(row[1], sheetTz);
    var vaptDate = cdvCellValueToYmd_(row[13], sheetTz);

    if (vaptProj !== pkExpect) {
      Logger.log(
        'cdvWriteCombinedSnykVaptBlock_: row ' +
          (r + 2) +
          ' VAPT Project mismatch (expected ' +
          pkExpect +
          ', got "' +
          vaptProj +
          '")'
      );
    }
    if (snykProj !== '' && vaptProj !== '' && snykProj !== vaptProj) {
      Logger.log(
        'cdvWriteCombinedSnykVaptBlock_: row ' +
          (r + 2) +
          ' SNYK/VAPT Project mismatch ("' +
          snykProj +
          '" vs "' +
          vaptProj +
          '")'
      );
    }
    if (snykDate !== '' && vaptDate !== '' && snykDate !== vaptDate) {
      Logger.log(
        'cdvWriteCombinedSnykVaptBlock_: row ' +
          (r + 2) +
          ' SNYK/VAPT Date mismatch (' +
          snykDate +
          ' vs ' +
          vaptDate +
          ')'
      );
    }

    var projOut = vaptProj || snykProj || pkExpect;
    var dateOut = vaptDate || snykDate;

    /* SNYK A–K: C,D,E,G,H,I,J — skip F (daily %), K (cum %) */
    var SK = { fix: 2, br: 3, within: 4, open: 6, cumF: 7, cumB: 8, cumW: 9 };
    /* VAPT M–W: same layout, indices +12 — skip R (daily %), W (cum %) */
    var VP = { fix: 14, br: 15, within: 16, open: 18, cumF: 19, cumB: 20, cumW: 21 };

    var cS = cdvParseNumericCell_(row[SK.fix]);
    var dS = cdvParseNumericCell_(row[SK.br]);
    var eS = cdvParseNumericCell_(row[SK.within]);
    var gS = cdvParseNumericCell_(row[SK.open]);
    var hS = cdvParseNumericCell_(row[SK.cumF]);
    var iS = cdvParseNumericCell_(row[SK.cumB]);
    var jS = cdvParseNumericCell_(row[SK.cumW]);

    var cV = cdvParseNumericCell_(row[VP.fix]);
    var dV = cdvParseNumericCell_(row[VP.br]);
    var eV = cdvParseNumericCell_(row[VP.within]);
    var gV = cdvParseNumericCell_(row[VP.open]);
    var hV = cdvParseNumericCell_(row[VP.cumF]);
    var iV = cdvParseNumericCell_(row[VP.cumB]);
    var jV = cdvParseNumericCell_(row[VP.cumW]);

    var sumFixed = cS + cV;
    var sumBreached = dS + dV;
    var sumWithin = eS + eV;
    var sumOpen = gS + gV;
    var cumF = hS + hV;
    var cumB = iS + iV;
    var cumW = jS + jV;

    var cc = cdvCompliancePct_(cumF, cumB);
    var ccStr = cumF > 0 && cc != null ? cc + '%' : '';

    combinedTable.push([
      projOut,
      dateOut,
      sumFixed,
      sumBreached,
      sumWithin,
      sumOpen,
      cumF,
      cumB,
      cumW,
      ccStr
    ]);
  }

  sheet.getRange(2, startCol, dataRows, numCombined).setValues(combinedTable);
  cdvFillHeaderRowIfEmpty_(sheet, startCol, numCombined, CDV_COMBINED_HEADERS);
}

function cdvOpenBreachedHyperlinkFormula_(url, displayCount) {
  var u = String(url).replace(/"/g, '""');
  var d = String(displayCount).replace(/"/g, '""');
  return '=HYPERLINK("' + u + '","' + d + '")';
}

/** Batch read/write: one getValues call instead of one getValue per header cell. */
function cdvFillHeaderRowIfEmpty_(sheet, startCol, numCols, headers) {
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

function cdvRowToDisplayRow_(row) {
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

function cdvEnsureSheet_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (sh) return sh;
  return ss.insertSheet(name);
}

function cdvGetBaseUrl_(props) {
  var u = props.getProperty('JIRA_BASE_URL');
  if (!u) throw new Error('Set JIRA_BASE_URL in script properties');
  return String(u)
    .trim()
    .replace(/\/+$/, '');
}

function cdvAuthHeaders_(props) {
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

function cdvGetProjectKeys_(props) {
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
  return CDV_MAPPING_PROJECT_KEYS_DEFAULT.slice();
}

function cdvProbeProjectKey_(projectKeys) {
  for (var i = 0; i < projectKeys.length; i++) {
    if (projectKeys[i] === 'OAA') return 'OAA';
  }
  return projectKeys.length ? projectKeys[0] : 'OAA';
}

function cdvBuildMainJql_(baseScope, resolvedGteYmd, resolvedLtYmd, projectKeys) {
  var parts = ['(' + baseScope + ')', CDV_LABEL_SCOPE_JQL, 'resolution IS NOT EMPTY'];
  if (projectKeys && projectKeys.length) {
    parts.push('project in (' + projectKeys.slice().sort().join(', ') + ')');
  }
  parts.push('resolved >= "' + resolvedGteYmd + '"');
  parts.push('resolved < "' + resolvedLtYmd + '"');
  return parts.join(' AND ');
}

function cdvResolveCustomFieldIds_(base, headers, probeJql, props) {
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
    for (var j = 0; j < CDV_INTRODUCED_NAMES.length; j++) {
      if (byName[CDV_INTRODUCED_NAMES[j]]) {
        introducedId = byName[CDV_INTRODUCED_NAMES[j]];
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
      severityId = cdvProbeSeverityFieldId_(base, headers, probeJql, candidates);
      if (!severityId) throw new Error('Multiple Severity fields; set JIRA_FIELD_SEVERITY');
    } else throw new Error('No Severity option field found');
  }

  return { severityId: severityId, introducedId: introducedId };
}

function cdvProbeSeverityFieldId_(base, headers, jql, candidateIds) {
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
      var v = cdvParseSeverityValue_(flds[sid]);
      if (v && CDV_SEVERITY_SLA_DAYS[v] != null) return sid;
    }
  }
  return null;
}

function cdvParseSeverityValue_(raw) {
  if (raw == null) return null;
  if (typeof raw === 'string') return raw.trim() || null;
  if (typeof raw === 'object' && raw.value) return String(raw.value).trim() || null;
  return null;
}

function cdvParseIntroducedDate_(raw) {
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

function cdvParseResolutionUtc_(raw) {
  if (raw == null) return null;
  var s = String(raw).trim();
  if (!s) return null;
  var d = new Date(s.replace(/Z$/, '+00:00'));
  if (isNaN(d.getTime())) return null;
  return d;
}

function cdvLocalDateString_(d, tz) {
  return Utilities.formatDate(d, tz, 'yyyy-MM-dd');
}

function cdvProjectKeyFromIssue_(issue) {
  var proj = (issue.fields || {}).project;
  if (!proj || typeof proj !== 'object') return null;
  return proj.key ? String(proj.key) : null;
}

function cdvAggregateIssues_(issues, tz, severityId, introducedId) {
  var counts = {};
  for (var i = 0; i < issues.length; i++) {
    var issue = issues[i];
    var pk = cdvProjectKeyFromIssue_(issue);
    if (!pk) continue;
    var fields = issue.fields || {};
    var labels = fields.labels || [];
    if (!Array.isArray(labels)) labels = [];
    var resUtc = cdvParseResolutionUtc_(fields.resolutiondate);
    if (resUtc == null) continue;
    var dstr = cdvLocalDateString_(resUtc, tz);
    var key = pk + '\t' + dstr;
    if (!counts[key]) counts[key] = { fixed: 0, breached: 0 };
    counts[key].fixed += 1;

    var sev = cdvParseSeverityValue_(fields[severityId]);
    var intro = cdvParseIntroducedDate_(fields[introducedId]);
    var resDate = cdvParseResolutionDateForSla_(fields.resolutiondate);
    var breachedInc = false;
    if (sev && intro && resDate && CDV_SEVERITY_SLA_DAYS[sev] != null) {
      var br = cdvSlaBreached_(intro, sev, resDate);
      breachedInc = br.breached;
    } else if (labels.indexOf(CDV_SLA_LABEL) >= 0) {
      breachedInc = true;
    }
    if (breachedInc) counts[key].breached += 1;
  }
  return counts;
}

function cdvParseResolutionDateForSla_(raw) {
  if (raw == null) return null;
  var s = String(raw).trim();
  if (!s) return null;
  var d = new Date(s.replace(/Z$/, '+00:00'));
  if (isNaN(d.getTime())) return null;
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

function cdvSlaBreached_(introduced, sev, resolved) {
  var n = CDV_SEVERITY_SLA_DAYS[sev];
  if (n == null) return { breached: false, n: null };
  var due = new Date(introduced.getTime());
  due.setUTCDate(due.getUTCDate() + n);
  return { breached: resolved.getTime() > due.getTime(), n: n };
}

function cdvCompliancePct_(fixed, breached) {
  if (fixed <= 0) return null;
  return Math.round(10000 * (1 - breached / fixed)) / 100;
}

function cdvApplyCumulative_(rows) {
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
    var cc = cdvCompliancePct_(cumF, cumB);
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

function cdvBuildOpenPastSlaJql_(baseScope, projectKey, snapshotYmd, doneStatuses) {
  // VAPT open-past-SLA: two-stage SLA using createdDate (not vulnerability_introduced_date).
  //   Stage 1 — not yet mitigated:       createdDate < -N days  AND "Mitigated At (Security)[Date]" is EMPTY
  //   Stage 2 — mitigated but still open: createdDate < -M days  AND "Mitigated At (Security)[Date]" is not EMPTY
  // Uses current status NOT IN (Done, Archived) — matches the actual working Jira query.
  var sevParts = [];
  var keys = Object.keys(CDV_SEVERITY_SLA_DAYS);
  for (var k = 0; k < keys.length; k++) {
    var sev = keys[k];
    var n1 = CDV_SEVERITY_SLA_DAYS[sev];
    var n2 = CDV_SEVERITY_SLA_STAGE2_DAYS[sev];
    sevParts.push(
      '("Severity[Dropdown]" = ' + sev +
      ' AND (createdDate < -' + n1 + 'd AND "Mitigated At (Security)[Date]" is EMPTY' +
      ' OR createdDate < -' + n2 + 'd AND "Mitigated At (Security)[Date]" is not EMPTY))'
    );
  }
  var inner = doneStatuses
    .map(function (s) { return s.trim(); })
    .filter(function (s) { return s; })
    .join(', ');
  var parts = [
    CDV_LABEL_SCOPE_JQL,
    'project = ' + projectKey,
    'status NOT IN (' + inner + ')',
    '(' + sevParts.join(' OR ') + ')'
  ];
  return parts.join(' AND ');
}

function cdvCountJqlIssues_(base, headers, jql, maxResults) {
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
  Logger.log('cdvCountJqlIssues_ JQL: ' + jql + ' → count=' + count);
  return count;
}

/**
 * @param {Object=} cacheOpt - optional shared cache (pk+date → count) across projects
 * @param {string=} onlyYmd  - when set, only fetches open-past-SLA for this date (today).
 *   Historical rows are left empty — they were written by previous daily runs and the
 *   HYPERLINK formula only ever uses today's count anyway. Reduces API calls from
 *   (projects × days) down to (projects × 1).
 */
function cdvAttachOpenPastSla_(base, headers, baseScope, rows, maxResults, sleepMs, cacheOpt, onlyYmd) {
  var cache = cacheOpt || {};
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var pk = String(row.Project);
    var dstr = String(row.Date);
    var ck = pk + '\t' + dstr;
    // Cache check FIRST: historical values preloaded from the sheet satisfy this.
    if (Object.prototype.hasOwnProperty.call(cache, ck)) {
      row.Open_Past_SLA_Count = cache[ck];
      continue;
    }
    // Skip the live Jira fetch for any date other than today.
    if (onlyYmd && dstr !== onlyYmd) continue;
    var jql = cdvBuildOpenPastSlaJql_(baseScope, pk, dstr, CDV_OPEN_DONE_STATUSES);
    var n = cdvCountJqlIssues_(base, headers, jql, maxResults);
    cache[ck] = n;
    row.Open_Past_SLA_Count = n;
    if (sleepMs > 0) Utilities.sleep(sleepMs);
  }
}

/**
 * Reads existing Open-Breached values from the VAPT block (col M onward) of the project sheet
 * into the shared cache so that cdvAttachOpenPastSla_ can serve historical dates without
 * re-querying Jira. One batch getValues call per project; skips today (fetched fresh).
 */
function cdvPreloadOpenCacheFromSheet_(ss, projectKey, endYmd, cache) {
  var sheet = ss.getSheetByName(projectKey);
  if (!sheet) return;
  var maxRows = sheet.getMaxRows();
  if (maxRows < 2) return;
  var sheetTz = ss.getSpreadsheetTimeZone();
  // VAPT block starts at col M (CDV_DATA_START_COL=13); read 7 cols M–S.
  // date = idx 1 (col N), Open-Breached = idx 6 (col S).
  var data = sheet.getRange(2, CDV_DATA_START_COL, maxRows - 1, 7).getValues();
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
    if (Object.prototype.hasOwnProperty.call(cache, ck)) continue;
    if (eOpen === '' || eOpen === null || eOpen === undefined) continue;
    var n = parseInt(eOpen, 10);
    if (!isNaN(n)) cache[ck] = n;
  }
}

function cdvTodayYmd_(tz) {
  return Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
}

function cdvIterDatesInclusive_(startYmd, endYmd) {
  var out = [];
  var cur = new Date(startYmd + 'T12:00:00Z');
  var end = new Date(endYmd + 'T12:00:00Z');
  while (cur.getTime() <= end.getTime()) {
    out.push(Utilities.formatDate(cur, 'UTC', 'yyyy-MM-dd'));
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
  return out;
}

function cdvAddDaysYmd_(ymd, deltaDays) {
  var p = ymd.split('-');
  var d = new Date(Date.UTC(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10)));
  d.setUTCDate(d.getUTCDate() + deltaDays);
  var y = d.getUTCFullYear();
  var m = ('0' + (d.getUTCMonth() + 1)).slice(-2);
  var day = ('0' + d.getUTCDate()).slice(-2);
  return y + '-' + m + '-' + day;
}

function cdvCompareYmd_(a, b) {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

/**
 * One-time backfill: for every project tab, fills empty Open-Breached (col S) cells in the VAPT
 * block (col M onward) for historical dates. Fetches all relevant VAPT issues once per project
 * and computes point-in-time open-past-SLA counts in memory (two-stage SLA).
 *
 * Run via "Jira compliance VAPT → Backfill historical Open-Breached VAPT (one-time)".
 * Progress saved in CDV_BACKFILL_COMPLETED so a GAS-timeout mid-run can be resumed.
 */
function runBackfillOpenBreachedVapt_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('Run from a container-bound spreadsheet');

  var props = PropertiesService.getScriptProperties();
  var base = cdvGetBaseUrl_(props);
  var authHdrs = cdvAuthHeaders_(props);
  var tz = (props.getProperty('JIRA_COMPLIANCE_TIMEZONE') || 'UTC').trim() || 'UTC';
  var startYmd = (props.getProperty('JIRA_COMPLIANCE_START') || CDV_DEFAULT_START).trim();
  var endYmd = cdvTodayYmd_(tz);
  var projectKeys = cdvGetProjectKeys_(props);
  var sheetTz = ss.getSpreadsheetTimeZone();
  var openCol = CDV_DATA_START_COL + 6; // col S = 19

  // Resolve field IDs (severity + mitigation date).
  var fieldResp = UrlFetchApp.fetch(base + '/rest/api/3/field', {
    method: 'get', headers: authHdrs, muteHttpExceptions: true
  });
  if (fieldResp.getResponseCode() !== 200) throw new Error('GET /field failed: ' + fieldResp.getContentText().substring(0, 300));
  var allFields = JSON.parse(fieldResp.getContentText());

  var severityCandidates = [];
  var byName = {};
  for (var f = 0; f < allFields.length; f++) {
    var fld = allFields[f];
    if (fld.name) byName[fld.name] = fld.id;
    if (fld.name === 'Severity' && fld.schema && fld.schema.type === 'option') severityCandidates.push(fld.id);
  }
  var severityId = null;
  if (severityCandidates.length === 1) {
    severityId = severityCandidates[0];
  } else if (severityCandidates.length > 1) {
    var probeJql = 'project = ' + cdvProbeProjectKey_(projectKeys) + ' AND ' + CDV_LABEL_SCOPE_JQL + ' ORDER BY updated DESC';
    severityId = cdvProbeSeverityFieldId_(base, authHdrs, probeJql, severityCandidates);
  }
  if (!severityId) throw new Error('Could not resolve Severity field ID for VAPT backfill');

  var mitigatedAtId = byName['Mitigated At (Security)[Date]'] || null;
  if (!mitigatedAtId) {
    for (var fn in byName) {
      if (byName.hasOwnProperty(fn) && fn.toLowerCase().indexOf('mitigated at') >= 0) {
        mitigatedAtId = byName[fn];
        break;
      }
    }
  }
  if (!mitigatedAtId) Logger.log('VAPT backfill: WARNING — Mitigated At field not found; stage-2 SLA not applied');

  var issueFields = ['key', 'created', 'project', 'resolutiondate', severityId];
  if (mitigatedAtId) issueFields.push(mitigatedAtId);

  var completedStr = props.getProperty('CDV_BACKFILL_COMPLETED') || '';
  var completed = completedStr ? completedStr.split(',').filter(function(s) { return s; }) : [];

  var totalFilled = 0;

  for (var pi = 0; pi < projectKeys.length; pi++) {
    var pk = projectKeys[pi];
    if (completed.indexOf(pk) >= 0) {
      Logger.log('VAPT backfill: skipping ' + pk + ' (already done)');
      continue;
    }

    var sheet = ss.getSheetByName(pk);
    if (!sheet) {
      Logger.log('VAPT backfill: no sheet for ' + pk);
      completed.push(pk);
      props.setProperty('CDV_BACKFILL_COMPLETED', completed.join(','));
      continue;
    }

    var maxRows = sheet.getMaxRows();
    if (maxRows < 2) {
      completed.push(pk);
      props.setProperty('CDV_BACKFILL_COMPLETED', completed.join(','));
      continue;
    }

    // VAPT block: read 7 cols starting at col M (CDV_DATA_START_COL=13).
    // date = idx 1 (col N), Open-Breached = idx 6 (col S).
    var sheetData = sheet.getRange(2, CDV_DATA_START_COL, maxRows - 1, 7).getValues();
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
      if (!eDateStr || eDateStr === endYmd) continue;
      needBackfill[eDateStr] = ri;
    }

    var needDates = Object.keys(needBackfill);
    if (needDates.length === 0) {
      Logger.log('VAPT backfill: ' + pk + ' — no rows need backfill, skipping');
      completed.push(pk);
      props.setProperty('CDV_BACKFILL_COMPLETED', completed.join(','));
      continue;
    }

    Logger.log('VAPT backfill: ' + pk + ' — ' + needDates.length + ' dates need fill; fetching issues…');

    // VAPT open-past-SLA queries do NOT use baseScope (matches cdvBuildOpenPastSlaJql_ behaviour).
    var backfillJql = [
      CDV_LABEL_SCOPE_JQL,
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
        throw new Error('VAPT backfill search failed for ' + pk + ': ' + resp.getResponseCode() + ' ' + resp.getContentText().substring(0, 400));
      }
      var pageData = JSON.parse(resp.getContentText());
      var batch = pageData.issues || [];
      for (var b = 0; b < batch.length; b++) issues.push(batch[b]);
      nextToken = pageData.nextPageToken;
      if (pageData.isLast || !nextToken) break;
    } while (true);

    Logger.log('VAPT backfill: ' + pk + ' — ' + issues.length + ' issues fetched');

    var issueData = [];
    for (var ii = 0; ii < issues.length; ii++) {
      var flds = issues[ii].fields || {};
      var createdFull = cdvParseResolutionUtc_(flds.created);
      if (!createdFull) continue;
      var createdDayMs = Date.UTC(createdFull.getUTCFullYear(), createdFull.getUTCMonth(), createdFull.getUTCDate());
      var resolvedFull = cdvParseResolutionUtc_(flds.resolutiondate);
      var resolvedDayMs = resolvedFull
        ? Date.UTC(resolvedFull.getUTCFullYear(), resolvedFull.getUTCMonth(), resolvedFull.getUTCDate())
        : null;
      var sev = cdvParseSeverityValue_(flds[severityId]);
      if (!sev || CDV_SEVERITY_SLA_DAYS[sev] == null) continue;
      var sla1Ms = CDV_SEVERITY_SLA_DAYS[sev] * 86400000;
      var sla2Ms = CDV_SEVERITY_SLA_STAGE2_DAYS[sev] * 86400000;
      var mitigatedDayMs = null;
      if (mitigatedAtId && flds[mitigatedAtId]) {
        // cdvParseIntroducedDate_ handles both string and object date formats
        var mitDate = cdvParseIntroducedDate_(flds[mitigatedAtId]);
        if (mitDate) mitigatedDayMs = mitDate.getTime();
      }
      issueData.push({ createdDayMs: createdDayMs, resolvedDayMs: resolvedDayMs, sla1Ms: sla1Ms, sla2Ms: sla2Ms, mitigatedDayMs: mitigatedDayMs });
    }

    Logger.log('VAPT backfill: ' + pk + ' — ' + issueData.length + ' issues with SLA data');

    for (var di = 0; di < needDates.length; di++) {
      var dstr = needDates[di];
      var dp = dstr.split('-');
      var dMs = Date.UTC(parseInt(dp[0], 10), parseInt(dp[1], 10) - 1, parseInt(dp[2], 10));
      var count = 0;
      for (var ij = 0; ij < issueData.length; ij++) {
        var iss = issueData[ij];
        // Issue must have existed and been open on date D.
        if (iss.createdDayMs > dMs) continue;
        if (iss.resolvedDayMs !== null && iss.resolvedDayMs <= dMs) continue;
        // Stage 1: not yet mitigated on D AND created + stage1_SLA < D
        var notMitigOnD = iss.mitigatedDayMs === null || iss.mitigatedDayMs > dMs;
        if (notMitigOnD && (iss.createdDayMs + iss.sla1Ms) < dMs) { count++; continue; }
        // Stage 2: mitigated on or before D AND created + stage2_SLA < D
        var mitigOnD = iss.mitigatedDayMs !== null && iss.mitigatedDayMs <= dMs;
        if (mitigOnD && (iss.createdDayMs + iss.sla2Ms) < dMs) count++;
      }
      sheet.getRange(2 + needBackfill[dstr], openCol).setValue(count);
      totalFilled++;
    }

    SpreadsheetApp.flush();
    Logger.log('VAPT backfill: ' + pk + ' done — wrote ' + needDates.length + ' cells (' + (pi + 1) + '/' + projectKeys.length + ')');
    completed.push(pk);
    props.setProperty('CDV_BACKFILL_COMPLETED', completed.join(','));
  }

  props.deleteProperty('CDV_BACKFILL_COMPLETED');

  try {
    SpreadsheetApp.getUi().alert(
      'VAPT Open-Breached backfill complete',
      'Filled ' + totalFilled + ' historical Open-Breached cells across ' + projectKeys.length + ' projects.\n\n' +
      'Run "Refresh VAPT report" to confirm the chart trend is restored.',
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (e) {
    Logger.log('VAPT backfill done: ' + totalFilled + ' cells filled');
  }
}
