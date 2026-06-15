/**
 * Dev9 CMS sanity — daily Google Sheet sync from Slack (Apps Script port of
 * dev9-build-send-cms-table-jenkins-unified.py).
 *
 * Each calendar weekday D uses Slack messages from D 00:00:00 through D 23:59:59.999
 * (script timezone — i.e. [D00:00:00, D+1 00:00:00)). Weekends are skipped (no column update).
 *
 * SETUP (Extensions → Apps Script, paste this file):
 * 1. Script Properties (Project Settings → Script properties):
 *    - SLACK_BOT_TOKEN   (xoxb-...)
 *    - SLACK_CHANNEL_ID  (C...)
 *    Optional:
 *    - SPREADSHEET_ID    (only if the script is not bound to the target spreadsheet)
 *    - SHEET_STATUS, SHEET_FAILURES, SHEET_TOTAL_RUNS (defaults: FY27SanityStatus, FY27Failures, FY27TotalRuns)
 * 2. File → Project settings → set Time zone (defines each “day” for 00:00–23:59 filtering).
 * 3. Run once: setupDailyMorningTrigger() — creates a daily trigger ~7:00 for "yesterday".
 *    Or change the hour in that function. Authorize Slack + Spreadsheet scopes when prompted.
 *
 * MANUAL: Run runDailyUpdateForPreviousDay() from the editor to test.
 *         Run runForCalendarDate('2026-04-15') to backfill one weekday.
 */

var PASS_MARK = '\u2705';
var FAIL_MARK = '\u274c';

var DEFAULT_SHEET_STATUS = 'FY27SanityStatus';
var DEFAULT_SHEET_FAILURES = 'FY27Failures';
var DEFAULT_SHEET_TOTAL_RUNS = 'FY27TotalRuns';

/** Same default order as Python dev9-build-send-cms-table-jenkins-unified.py */
var ALL_ORDER = [
  'Full Sanity - UI',
  'Assets Full Sanity - UI',
  'Releases20 Full Sanity - UI',
  'CMA Full Sanity - API',
  'BulkDelete - API',
  'Release20 - API',
  'AssetManagement20 - API',
  'Asset Managment Test - UI',
  'AssetPicker Full Sanity - UI',
];

// --- Entry points ---

function runDailyUpdateForPreviousDay() {
  var tz = Session.getScriptTimeZone();
  var now = new Date();
  var y = new Date(now.getTime());
  y.setDate(y.getDate() - 1);
  var prev = calendarDateOnly(y, tz);
  syncWeekdayReport(prev);
}

/**
 * Optional: bind a time-driven trigger to this (no args).
 * @param {number} hour Local hour 0–23 (default7).
 */
function setupDailyMorningTrigger(hour) {
  hour = hour == null ? 7 : hour;
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runDailyUpdateForPreviousDay') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('runDailyUpdateForPreviousDay')
    .timeBased()
    .everyDays(1)
    .atHour(hour)
    .create();
}

/**
 * @param {string} ymd YYYY-MM-DD
 */
function runForCalendarDate(ymd) {
  var d = parseYmd_(ymd);
  syncWeekdayReport(d);
}

// --- Core ---

/**
 * @param {Date} reportDay Calendar date (time ignored); must be weekday or no-op.
 */
function syncWeekdayReport(reportDay) {
  var tz = Session.getScriptTimeZone();
  if (weekday_(reportDay, tz) >= 5) {
    Logger.log('Skip: report day is weekend — ' + Utilities.formatDate(reportDay, tz, 'yyyy-MM-dd EEEE'));
    return;
  }

  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('SLACK_BOT_TOKEN');
  var channel = props.getProperty('SLACK_CHANNEL_ID');
  if (!token || !channel) {
    throw new Error('Set Script properties SLACK_BOT_TOKEN and SLACK_CHANNEL_ID.');
  }

  var rangeStart = startOfCalendarDay_(reportDay, tz);
  var rangeEndExclusive = startOfCalendarDay_(addCalendarDays_(rangeStart, 1), tz);

  var oldest = rangeStart.getTime() / 1000;
  var latest = rangeEndExclusive.getTime() / 1000;

  var events = fetchSlackEvents_(
    token,
    channel,
    oldest,
    latest,
    rangeStart,
    rangeEndExclusive
  );
  var seen = {};
  events.forEach(function (e) {
    var nk = normalizeSuiteName(e.suite);
    if (nk) seen[nk] = true;
  });
  var order = buildOutputOrder_(seen);

  var reportDates = [calendarDateOnly(reportDay, tz)];
  var metrics = buildDailyMetrics_(events, reportDates, reportDay, reportDay, order);

  var dh = Utilities.formatDate(reportDay, tz, 'MM/dd');
  var dKey = Utilities.formatDate(reportDay, tz, 'yyyy-MM-dd');
  var suiteStatus = {};
  var suiteFails = {};
  var suiteTotals = {};
  order.forEach(function (s) {
    suiteStatus[s] = String(metrics[dKey][s].last);
    suiteFails[s] = metrics[dKey][s].failures;
    suiteTotals[s] = metrics[dKey][s].total;
  });

  var ss = getSpreadsheet_();
  var sn = props.getProperty('SHEET_STATUS') || DEFAULT_SHEET_STATUS;
  var fn = props.getProperty('SHEET_FAILURES') || DEFAULT_SHEET_FAILURES;
  var tn = props.getProperty('SHEET_TOTAL_RUNS') || DEFAULT_SHEET_TOTAL_RUNS;

  uploadStatusColumn_(ss.getSheetByName(sn), dh, suiteStatus);
  Utilities.sleep(3000);
  updateCountsColumn_(ss.getSheetByName(fn), dh, suiteFails, 'failures');
  Utilities.sleep(3000);
  updateCountsColumn_(ss.getSheetByName(tn), dh, suiteTotals, 'total');

  Logger.log('Done: ' + dh + ' — ' + events.length + ' Slack rows processed.');
}

// --- Slack ---

/**
 * @param {Date} rangeStart Inclusive start (local midnight of report day).
 * @param {Date} rangeEndExclusive Next calendar day 00:00:00 (messages must be < this).
 */
function fetchSlackEvents_(token, channel, oldest, latest, rangeStart, rangeEndExclusive) {
  var out = [];
  var cursor = '';
  do {
    var payload = {
      channel: channel,
      oldest: String(oldest),
      latest: String(latest),
      limit: 100,
    };
    if (cursor) payload.cursor = cursor;

    var resp = UrlFetchApp.fetch('https://slack.com/api/conversations.history', {
      method: 'post',
      contentType: 'application/x-www-form-urlencoded',
      headers: { Authorization: 'Bearer ' + token },
      payload: payload,
      muteHttpExceptions: true,
    });

    var data = JSON.parse(resp.getContentText());
    if (!data.ok) {
      throw new Error('Slack API error: ' + (data.error || resp.getContentText()));
    }

    var messages = data.messages || [];
    for (var i = 0; i < messages.length; i++) {
      var message = messages[i];
      var ts = parseFloat(message.ts);
      var messageDateObj = new Date(ts * 1000);
      if (messageDateObj < rangeStart || messageDateObj >= rangeEndExclusive) continue;

      var text = extractMessageText_(message);
      var tl = text.toLowerCase();
      var firstLine = '';
      var secondLine = '';

      var looksLikeResult =
        tl.indexOf('dev9') !== -1 &&
        (tl.indexOf('result') !== -1 ||
          tl.indexOf(':x:') !== -1 ||
          tl.indexOf(':white_check_mark:') !== -1 ||
          tl.indexOf('failure') !== -1 ||
          tl.indexOf('passed') !== -1 ||
          tl.indexOf('success') !== -1);

      if (looksLikeResult) {
        var lines = text
          .split('\n')
          .map(function (ln) {
            return ln.trim();
          })
          .filter(function (ln) {
            return ln;
          });
        if (lines.length) {
          var lpLegacyShift =
            text.indexOf('Live Preview') !== -1 &&
            lines.length >= 2 &&
            lines[0].toLowerCase().indexOf('dev9') === -1;
          if (lpLegacyShift) {
            firstLine = lines[1];
            secondLine = lines.length > 2 ? lines[2] : '';
          } else {
            var devIdx = -1;
            for (var li = 0; li < lines.length; li++) {
              if (lines[li].toLowerCase().indexOf('dev9') !== -1) {
                devIdx = li;
                break;
              }
            }
            if (devIdx !== -1) {
              firstLine = lines[devIdx];
              var resultIdx = null;
              for (var j = devIdx + 1; j < lines.length; j++) {
                var lj = lines[j].toLowerCase();
                if (
                  lj.indexOf('result') !== -1 ||
                  lj.indexOf(':x:') !== -1 ||
                  lj.indexOf(':white_check_mark:') !== -1 ||
                  lj.indexOf('failure') !== -1 ||
                  lj.indexOf('fail') !== -1 ||
                  lj.indexOf('passed') !== -1 ||
                  lj.indexOf('success') !== -1 ||
                  lj.indexOf('failed') === 0
                ) {
                  resultIdx = j;
                  break;
                }
              }
              if (resultIdx != null) secondLine = lines[resultIdx];
              else if (devIdx + 1 < lines.length) secondLine = lines[devIdx + 1];
            }
          }
        }
      }

      if (!firstLine && message.blocks) {
        var fl = '';
        var sl = '';
        for (var bi = 0; bi < message.blocks.length; bi++) {
          var block = message.blocks[bi];
          var btype = block.type;
          if (btype === 'section' || btype === 'header') {
            var blockText = (((block.text || {}).text) || '').trim();
            var linesB = blockText.split('\n');
            if (!linesB.some(function (x) { return x.trim(); })) continue;

            if (
              blockText.indexOf('Live Preview') !== -1 &&
              linesB.length > 1 &&
              (linesB[0] || '').toLowerCase().indexOf('dev9') === -1
            ) {
              fl = linesB.length > 1 ? linesB[1].trim() : '';
              sl = linesB.length > 2 ? linesB[2].trim() : '';
              break;
            }
            if (linesB.length > 0 && linesB[0].toLowerCase().indexOf('dev9') !== -1) {
              fl = linesB[0].trim();
              sl = linesB.length > 1 ? linesB[1].trim() : '';
              break;
            }
            if (!fl && linesB.length > 0) fl = linesB[0].trim();
            else if (fl && linesB.length > 0) {
              sl = linesB[0].trim();
              break;
            }
          } else if (btype === 'section' && fl) {
            var bt2 = (((block.text || {}).text) || '').trim();
            if (bt2) {
              sl = bt2;
              break;
            }
          }
        }
        firstLine = fl;
        secondLine = sl;
      }

      if (
        firstLine &&
        !/<@|report|Azure-eu/i.test(firstLine)
      ) {
        out.push({ suite: firstLine, status: secondLine, dt: messageDateObj });
      }
    }

    cursor = (data.response_metadata && data.response_metadata.next_cursor) || '';
  } while (cursor);

  return out;
}

function extractMessageText_(message) {
  var chunks = [];
  if (message.text) chunks.push(String(message.text));
  var blocks = message.blocks || [];
  for (var i = 0; i < blocks.length; i++) {
    var block = blocks[i];
    var btype = block.type;
    if (btype === 'rich_text') {
      var elements = block.elements || [];
      for (var e = 0; e < elements.length; e++) {
        var el = elements[e];
        if (el.type === 'rich_text_section') {
          var subs = el.elements || [];
          for (var s = 0; s < subs.length; s++) {
            var sub = subs[s];
            if (sub.type === 'text') chunks.push(sub.text || '');
          }
        }
      }
    } else if (btype === 'section' || btype === 'header') {
      var bt = block.text || {};
      if (bt.text) chunks.push(String(bt.text));
    }
  }
  return chunks
    .filter(function (c) {
      return c;
    })
    .join('\n');
}

// --- Normalization / metrics (mirror Python) ---

var STATUS_REPLACEMENTS = [
  [/\*Result\*:/gi, ''],
  [/dev9,/gi, ''],
  [/dev9 ,/gi, ''],
  [/Dev9,/gi, ''],
  [/Passed/gi, ''],
  [/Success/gi, ''],
  [/Failure/gi, ''],
  [/\bResult\b/gi, ''],
  [/\bModules\b/gi, ''],
  [/\(/g, ''],
  [/\)/g, ''],
  [/:/g, ''],
  [/tests passed/gi, ''],
  [/"/g, ''],
  [/mins/gi, 'm'],
  [/sec\./gi, 's'],
  [/\*/g, ''],
];

function normalizeStatusText(raw) {
  if (!raw || !String(raw).trim()) return '';
  var s = String(raw);
  s = s.replace(/:white_check_mark:/gi, PASS_MARK);
  s = s.replace(/:x:/gi, FAIL_MARK);
  s = s.replace(/:X:/g, FAIL_MARK);
  for (var i = 0; i < STATUS_REPLACEMENTS.length; i++) {
    s = s.replace(STATUS_REPLACEMENTS[i][0], STATUS_REPLACEMENTS[i][1]);
  }
  return s.trim();
}

function classifyOutcome(normalized) {
  if (!normalized) return 'unknown';
  if (normalized.indexOf(PASS_MARK) !== -1 || /\bpass(ed)?\b/i.test(normalized)) return 'pass';
  if (normalized.indexOf(FAIL_MARK) !== -1 || /\bfail/i.test(normalized)) return 'fail';
  return 'unknown';
}

function lastStatusSymbol(outcomes) {
  for (var i = outcomes.length - 1; i >= 0; i--) {
    if (outcomes[i] === 'pass') return PASS_MARK;
    if (outcomes[i] === 'fail') return FAIL_MARK;
  }
  return '';
}

function normalizeSuiteName(raw) {
  var s = String(raw || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\u2007/g, ' ')
    .replace(/\u202f/g, ' ')
    .trim();
  s = s.replace(/^\*+/, '').replace(/\*+$/, '');
  s = s.replace(/^dev9\s*,\s*/i, '').replace(/^dev9\s+/i, '').trim();
  var sl = s.toLowerCase();
  if (sl.indexOf('graphql preview service') !== -1) s = s.replace(/\*/g, '');
  else if (sl.indexOf('rest preview service') !== -1) s = s.replace(/\*/g, '');
  s = s.replace(/\s+/g, ' ').trim();
  return s;
}

function buildOutputOrder_(seenSet) {
  var extras = Object.keys(seenSet)
    .filter(function (s) {
      return s && ALL_ORDER.indexOf(s) === -1;
    })
    .sort();
  return ALL_ORDER.concat(extras);
}

function outcomeFromStatus(raw) {
  var normalized = normalizeStatusText(raw);
  var c = classifyOutcome(normalized);
  if (c !== 'unknown') return c;
  var rawL = (raw || '').toLowerCase();
  if (
    rawL.indexOf(':x:') !== -1 ||
    rawL.indexOf(':heavy_multiplication_x:') !== -1 ||
    rawL.indexOf('failure') !== -1
  )
    return 'fail';
  if (
    rawL.indexOf(':white_check_mark:') !== -1 ||
    rawL.indexOf(':heavy_check_mark:') !== -1 ||
    rawL.indexOf(':large_green_circle:') !== -1
  )
    return 'pass';
  return 'unknown';
}

function buildDailyMetrics_(events, reportDates, rangeStartD, rangeEndD, order) {
  var tz = Session.getScriptTimeZone();
  var bucket = {};
  var startK = Utilities.formatDate(rangeStartD, tz, 'yyyy-MM-dd');
  var endK = Utilities.formatDate(rangeEndD, tz, 'yyyy-MM-dd');

  function keyForDateStr(dk, suite) {
    return dk + '\0' + suite;
  }

  for (var e = 0; e < events.length; e++) {
    var ev = events[e];
    var d = calendarDateOnly(ev.dt, tz);
    var dk = Utilities.formatDate(d, tz, 'yyyy-MM-dd');
    if (dk < startK || dk > endK) continue;
    if (weekday_(d, tz) >= 5) continue;
    if (!reportDates.some(function (rd) { return sameCalendar_(rd, d, tz); })) continue;
    var suiteK = normalizeSuiteName(ev.suite);
    if (!suiteK) continue;
    var outcome = outcomeFromStatus(ev.status);
    var k = keyForDateStr(dk, suiteK);
    if (!bucket[k]) bucket[k] = [];
    bucket[k].push({ outcome: outcome, dt: ev.dt });
  }

  for (var bk in bucket) {
    bucket[bk].sort(function (a, b) {
      return a.dt - b.dt;
    });
  }

  var metrics = {};
  for (var rj = 0; rj < reportDates.length; rj++) {
    var day = reportDates[rj];
    var dayKey = Utilities.formatDate(day, tz, 'yyyy-MM-dd');
    metrics[dayKey] = {};
    for (var oi = 0; oi < order.length; oi++) {
      var suite = order[oi];
      var k2 = keyForDateStr(dayKey, suite);
      var entries = bucket[k2] || [];
      var outcomes = entries.map(function (x) { return x.outcome; });
      metrics[dayKey][suite] = {
        last: lastStatusSymbol(outcomes),
        total: entries.length,
        failures: outcomes.filter(function (x) { return x === 'fail'; }).length,
      };
    }
  }
  return metrics;
}

// --- Sheets ---

function getSpreadsheet_() {
  var id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (id) return SpreadsheetApp.openById(id);
  return SpreadsheetApp.getActiveSpreadsheet();
}

function uploadStatusColumn_(sheet, dateHeader, suiteToStatus) {
  if (!sheet) throw new Error('Missing status sheet tab.');
  var data = sheet.getDataRange().getValues();
  if (!data.length) {
    Logger.log('Status sheet empty; skip.');
    return;
  }
  var header = data[0].map(function (c) { return c; });
  var colIndex = header.indexOf(dateHeader);
  if (colIndex === -1) {
    colIndex = header.length;
    sheet.getRange(1, colIndex + 1).setValue(dateHeader);
    header.push(dateHeader);
  }
  for (var r = 1; r < data.length; r++) {
    var row = data[r];
    var rowValue = row[0] ? String(row[0]).trim() : '';
    var nk = normalizeSuiteName(rowValue);
    var statusVal = suiteToStatus[nk];
    if (statusVal == null) statusVal = suiteToStatus[rowValue];
    if (statusVal == null) continue;
    sheet.getRange(r + 1, colIndex + 1).setValue(statusVal);
  }
}

function updateCountsColumn_(sheet, dateHeader, suiteToCount, mode) {
  if (!sheet) throw new Error('Missing counts sheet tab (' + mode + ').');
  var data = sheet.getDataRange().getValues();
  if (!data.length) {
    Logger.log('Sheet empty; skip ' + mode + '.');
    return;
  }
  var header = data[0].map(function (c) { return c; });
  var colIndex = header.indexOf(dateHeader);
  if (colIndex === -1) {
    colIndex = header.length;
    sheet.getRange(1, colIndex + 1).setValue(dateHeader);
    header.push(dateHeader);
  }
  for (var r = 1; r < data.length; r++) {
    var row = data[r];
    if (!row || !row[0]) continue;
    var testName = String(row[0]).trim();
    var nk = normalizeSuiteName(testName);
    var val = suiteToCount[nk];
    if (val == null) val = suiteToCount[testName];
    if (val == null) val = 0;
    sheet.getRange(r + 1, colIndex + 1).setValue(Number(val));
  }
}

// --- Date helpers ---

/** Local midnight 00:00:00 for the calendar day of d (in tz). */
function startOfCalendarDay_(d, tz) {
  var ymd = Utilities.formatDate(d, tz, 'yyyy-MM-dd');
  return Utilities.parseDate(ymd + ' 00:00:00', tz, 'yyyy-MM-dd HH:mm:ss');
}

/**
 * Add calendar days in the script timezone (handles DST via Date#setDate).
 * @param {Date} startMidnight A Date at 00:00:00 for some calendar day.
 * @param {number} days
 */
function addCalendarDays_(startMidnight, days) {
  var t = new Date(startMidnight.getTime());
  t.setDate(t.getDate() + days);
  return t;
}

function calendarDateOnly(d, tz) {
  return startOfCalendarDay_(d, tz);
}

function weekday_(d, tz) {
  return parseInt(Utilities.formatDate(d, tz, 'u'), 10) - 1;
}

function sameCalendar_(a, b, tz) {
  return Utilities.formatDate(a, tz, 'yyyy-MM-dd') === Utilities.formatDate(b, tz, 'yyyy-MM-dd');
}

function parseYmd_(ymd) {
  var tz = Session.getScriptTimeZone();
  var p = Utilities.parseDate(ymd + ' 00:00:00', tz, 'yyyy-MM-dd HH:mm:ss');
  if (!p || isNaN(p.getTime())) throw new Error('Bad date: ' + ymd);
  return p;
}
