/**
 * Jira SLA breach label automation (Apps Script port of jira_sla_breach_label.py).
 *
 * Adds label "sla-breached" to Done SCA/SAST fixable issues when resolution is after
 * SLA due (introduced_date + N days by severity: 14/30/90/180).
 *
 * SETUP (Project Settings → Script properties):
 *   JIRA_BASE_URL    e.g. https://your-domain.atlassian.net  (no trailing slash)
 *   JIRA_EMAIL       Atlassian account email
 *   JIRA_API_TOKEN   From https://id.atlassian.com/manage-profile/security/api-tokens
 * Optional:
 *   JIRA_FIELD_SEVERITY              e.g. customfield_10115
 *   JIRA_FIELD_VULNERABILITY_INTRODUCED  e.g. customfield_13646
 *   JIRA_SLA_PROJECT_KEYS            comma-separated keys; if set, overrides the list below
 *
 * RUN: runSlaBreachLabelDryRun_()  or  runSlaBreachLabelApply_()
 * Or time-driven trigger on runSlaBreachLabelApply_().
 *
 * Uses POST /rest/api/3/search/jql (same as Python). Default dry-run logs only.
 */

var SLA_LABEL = 'sla-breached';

var SEVERITY_SLA_DAYS = {
  'Sev-0': 14,
  'Sev-1': 30,
  'Sev-2': 90,
  'Sev-3': 180
};

var INTRODUCED_FIELD_NAMES = ['vulnerability_introduced_date', 'vulnerability_introduced_date[Date]'];

/**
 * Unique jiraProject values from mappings.json (MKT listed once).
 * Update this array when mappings.json changes, or set JIRA_SLA_PROJECT_KEYS in properties.
 */
var JIRA_SLA_MAPPING_PROJECT_KEYS_DEFAULT = [
  'AH', 'AM', 'DAT', 'OAA', 'CMA', 'CD', 'RS', 'ECL', 'CL', 'MKT', 'CSI', 'DX', 'AO', 'SS',
  'VB', 'VP', 'COMS', 'GROW', 'EXP'
];

/** Menu: Extensions → Jira SLA breach label */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Jira SLA breach')
    .addItem('Dry run (log only)', 'runSlaBreachLabelDryRun_')
    .addItem('Apply labels', 'runSlaBreachLabelApply_')
    .addToUi();
}

function runSlaBreachLabelDryRun_() {
  runSlaBreachLabelAllProjects_(true);
}

function runSlaBreachLabelApply_() {
  var ui = SpreadsheetApp.getUi();
  var r = ui.alert(
    'Apply sla-breached labels?',
    'This will update Jira issues. Continue?',
    ui.ButtonSet.YES_NO
  );
  if (r !== ui.Button.YES) return;
  runSlaBreachLabelAllProjects_(false);
}

/**
 * @param {boolean} dryRun - if true, no PUT; only logs
 */
function runSlaBreachLabelAllProjects_(dryRun) {
  var base = getJiraBaseUrl_();
  var headers = jiraAuthHeaders_();
  var projectKeys = getProjectKeysToRun_();
  var probePk = getProbeProjectKey_(projectKeys);
  var probeJql =
    'project = ' +
    probePk +
    ' AND labels = "vulnerability/sca" AND labels = "vulnerability/fixable" ORDER BY updated DESC';

  var ids = resolveCustomFieldIds_(base, headers, probeJql);
  var maxResults = 50;
  var sleepMs = 150;

  var totals = {
    projects: projectKeys.length,
    labeled: 0,
    skipped: 0,
    errors: 0,
    byProject: {}
  };

  for (var p = 0; p < projectKeys.length; p++) {
    var jp = projectKeys[p];
    var jql = buildJqlForProject_(jp);
    var nextToken = null;
    var page = 0;
    var projStats = { issues: 0, labeled: 0, skip: 0, err: 0 };

    do {
      var body = {
        jql: jql,
        maxResults: maxResults,
        fields: ['summary', 'labels', 'resolutiondate', ids.severityId, ids.introducedId]
      };
      if (nextToken) body.nextPageToken = nextToken;

      var url = base + '/rest/api/3/search/jql';
      var resp = UrlFetchApp.fetch(url, {
        method: 'post',
        contentType: 'application/json',
        headers: headers,
        payload: JSON.stringify(body),
        muteHttpExceptions: true
      });
      var code = resp.getResponseCode();
      if (code < 200 || code >= 300) {
        Logger.log('Jira search failed ' + jp + ' HTTP ' + code + ' ' + resp.getContentText().substring(0, 500));
        totals.errors++;
        break;
      }
      var data = JSON.parse(resp.getContentText());
      var issues = data.issues || [];
      page++;
      for (var i = 0; i < issues.length; i++) {
        var out = processIssue_(
          issues[i],
          ids.severityId,
          ids.introducedId,
          base,
          headers,
          dryRun,
          sleepMs
        );
        projStats.issues++;
        if (out === 'labeled') {
          projStats.labeled++;
          totals.labeled++;
        } else if (out === 'error_update') {
          projStats.err++;
          totals.errors++;
        } else totals.skipped++;
      }
      nextToken = data.nextPageToken;
      if (data.isLast || !nextToken) break;
    } while (true);

    totals.byProject[jp] = projStats;
    Logger.log('Project ' + jp + ' pages=' + page + ' stats=' + JSON.stringify(projStats));
  }

  var msg =
    'SLA breach label run complete. dryRun=' +
    dryRun +
    ' labeled=' +
    totals.labeled +
    ' errors=' +
    totals.errors +
    ' (see Logger for per-project)';
  Logger.log(msg);
  try {
    SpreadsheetApp.getUi().alert(msg);
  } catch (e) {
    /* no UI (e.g. trigger) */
  }
}

/** Prefer OAA for field probe (matches Python); else first key. */
function getProbeProjectKey_(projectKeys) {
  for (var i = 0; i < projectKeys.length; i++) {
    if (projectKeys[i] === 'OAA') return 'OAA';
  }
  return projectKeys.length ? projectKeys[0] : 'OAA';
}

function getProjectKeysToRun_() {
  var raw = PropertiesService.getScriptProperties().getProperty('JIRA_SLA_PROJECT_KEYS');
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
  return JIRA_SLA_MAPPING_PROJECT_KEYS_DEFAULT.slice();
}

function buildJqlForProject_(projectKey) {
  return (
    'labels in ("vulnerability/sca", "vulnerability/sast") AND labels = "vulnerability/fixable" ' +
    'AND labels != "vulnerability/ignored" AND project = ' +
    projectKey +
    ' AND status = Done'
  );
}

function getJiraBaseUrl_() {
  var u = PropertiesService.getScriptProperties().getProperty('JIRA_BASE_URL');
  if (!u) throw new Error('Set script property JIRA_BASE_URL');
  return String(u)
    .trim()
    .replace(/\/+$/, '');
}

function jiraAuthHeaders_() {
  var props = PropertiesService.getScriptProperties();
  var email = props.getProperty('JIRA_EMAIL');
  var token = props.getProperty('JIRA_API_TOKEN');
  if (!email || !token) throw new Error('Set JIRA_EMAIL and JIRA_API_TOKEN in script properties');
  var enc = Utilities.base64Encode(email + ':' + token);
  return {
    Authorization: 'Basic ' + enc,
    Accept: 'application/json',
    'Content-Type': 'application/json'
  };
}

function resolveCustomFieldIds_(base, headers, probeJql) {
  var props = PropertiesService.getScriptProperties();
  var envSev = (props.getProperty('JIRA_FIELD_SEVERITY') || '').trim();
  var envIntro = (props.getProperty('JIRA_FIELD_VULNERABILITY_INTRODUCED') || '').trim();

  var url = base + '/rest/api/3/field';
  var resp = UrlFetchApp.fetch(url, { method: 'get', headers: headers, muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) throw new Error('GET /field failed: ' + resp.getContentText().substring(0, 300));
  var fields = JSON.parse(resp.getContentText());
  var byName = {};
  for (var i = 0; i < fields.length; i++) {
    var f = fields[i];
    if (f.name) byName[f.name] = f.id;
  }

  var introducedId = envIntro || null;
  if (!introducedId) {
    for (var j = 0; j < INTRODUCED_FIELD_NAMES.length; j++) {
      if (byName[INTRODUCED_FIELD_NAMES[j]]) {
        introducedId = byName[INTRODUCED_FIELD_NAMES[j]];
        break;
      }
    }
  }
  if (!introducedId) throw new Error('Could not find vulnerability_introduced_date field; set JIRA_FIELD_VULNERABILITY_INTRODUCED');

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
      severityId = probeSeverityFieldId_(base, headers, probeJql, candidates);
      if (!severityId) {
        throw new Error('Multiple Severity fields; set JIRA_FIELD_SEVERITY to the correct custom field id');
      }
    } else throw new Error('No Severity select field found');
  }

  return { severityId: severityId, introducedId: introducedId };
}

function probeSeverityFieldId_(base, headers, jql, candidateIds) {
  var body = { jql: jql, maxResults: 25, fields: candidateIds };
  var url = base + '/rest/api/3/search/jql';
  var resp = UrlFetchApp.fetch(url, {
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
      var v = parseSeverityValue_(flds[sid]);
      if (v && SEVERITY_SLA_DAYS[v] != null) return sid;
    }
  }
  return null;
}

function parseSeverityValue_(raw) {
  if (raw == null) return null;
  if (typeof raw === 'string') return raw.trim() || null;
  if (typeof raw === 'object' && raw.value) return String(raw.value).trim() || null;
  return null;
}

function parseIntroducedDate_(raw) {
  if (raw == null) return null;
  var s = String(raw).trim();
  if (!s) return null;
  if (s.indexOf('T') >= 0) {
    var d = new Date(s);
    if (isNaN(d.getTime())) return null;
    return utcMidnight_(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
  }
  var parts = s.substring(0, 10).split('-');
  if (parts.length !== 3) return null;
  return utcMidnight_(parseInt(parts[0], 10), parseInt(parts[1], 10), parseInt(parts[2], 10));
}

function parseResolutionDate_(raw) {
  if (raw == null) return null;
  var s = String(raw).trim();
  if (!s) return null;
  var d = new Date(s.replace(/Z$/, '+00:00'));
  if (isNaN(d.getTime())) return null;
  return utcMidnight_(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
}

function utcMidnight_(y, month1to12, day) {
  return new Date(Date.UTC(y, month1to12 - 1, day));
}

function addUtcDays_(d, n) {
  var x = new Date(d.getTime());
  x.setUTCDate(x.getUTCDate() + n);
  return x;
}

function slaBreached_(introduced, sev, resolved) {
  var n = SEVERITY_SLA_DAYS[sev];
  if (n == null) return { breached: false, n: null };
  var due = addUtcDays_(introduced, n);
  return { breached: resolved.getTime() > due.getTime(), n: n };
}

function processIssue_(issue, severityId, introducedId, base, headers, dryRun, sleepMs) {
  var key = issue.key || '?';
  var fields = issue.fields || {};
  var labels = fields.labels || [];
  labels = Array.isArray(labels) ? labels : [];
  if (labels.indexOf(SLA_LABEL) >= 0) return 'skip_has_label';

  var sev = parseSeverityValue_(fields[severityId]);
  if (!sev || SEVERITY_SLA_DAYS[sev] == null) {
    Logger.log(key + ': skip severity');
    return 'skip_bad_severity';
  }

  var introduced = parseIntroducedDate_(fields[introducedId]);
  if (!introduced) {
    Logger.log(key + ': skip no introduced');
    return 'skip_no_introduced';
  }

  var resolved = parseResolutionDate_(fields.resolutiondate);
  if (!resolved) {
    Logger.log(key + ': skip no resolution');
    return 'skip_no_resolution';
  }

  var br = slaBreached_(introduced, sev, resolved);
  if (!br.breached) return 'ok_within_sla';

  if (dryRun) {
    Logger.log('DRYRUN would add ' + SLA_LABEL + ' ' + key);
    return 'labeled';
  }

  var putUrl = base + '/rest/api/3/issue/' + encodeURIComponent(key);
  var putBody = { update: { labels: [{ add: SLA_LABEL }] } };
  var putResp = UrlFetchApp.fetch(putUrl, {
    method: 'put',
    contentType: 'application/json',
    headers: headers,
    payload: JSON.stringify(putBody),
    muteHttpExceptions: true
  });
  if (putResp.getResponseCode() < 200 || putResp.getResponseCode() >= 300) {
    Logger.log('PUT failed ' + key + ' ' + putResp.getResponseCode() + ' ' + putResp.getContentText().substring(0, 400));
    return 'error_update';
  }
  Logger.log('Added ' + SLA_LABEL + ' ' + key);
  if (sleepMs > 0) Utilities.sleep(sleepMs);
  return 'labeled';
}
