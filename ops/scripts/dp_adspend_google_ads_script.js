/**
 * AD_SPEND_2026_08_24 - push DealerPrice ad cost to Experience Wholesale.
 *
 * WHY A SCRIPT AND NOT THE GOOGLE ADS API: the API needs a developer token,
 * which needs a Manager (MCC) account and a written application Google reviews
 * by hand. This runs inside the account with no token and no OAuth setup.
 *
 * Schedule: HOURLY. Re-sends the last 7 days every run on purpose - Google
 * restates recent figures, so a repeat push self-heals any day that was read
 * mid-flight, and a missed run costs nothing.
 *
 * Shows up at https://experience-wholesale.net/network/visitors
 */
var ENDPOINT = 'https://experience-wholesale.net/api/dp/adspend';
var KEY = '__DP_ADSPEND_KEY__';   // dedicated key; grants ONLY dp_ad_spend writes
var DAYS_BACK = 7;

function main() {
  var tz = AdsApp.currentAccount().getTimeZone();
  var today = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd');
  var start = Utilities.formatDate(
      new Date(new Date().getTime() - DAYS_BACK * 86400000), tz, 'yyyy-MM-dd');

  // BETWEEN start AND today, not LAST_7_DAYS: the preset EXCLUDES today, and
  // today's spend is the whole point of putting this on the dashboard.
  var q = 'SELECT segments.date, metrics.cost_micros, metrics.clicks, ' +
          'metrics.impressions, metrics.conversions ' +
          'FROM customer ' +
          "WHERE segments.date BETWEEN '" + start + "' AND '" + today + "'";

  var rows = [];
  var it = AdsApp.search(q);
  while (it.hasNext()) {
    var r = it.next();
    var m = r.metrics || {};
    rows.push({
      date: r.segments.date,
      cost: Number(m.costMicros || 0) / 1000000,   // micros -> dollars
      clicks: Number(m.clicks || 0),
      impressions: Number(m.impressions || 0),
      conversions: Number(m.conversions || 0)
    });
  }

  if (!rows.length) {
    Logger.log('AD_SPEND: no rows for ' + start + '..' + today + ' - nothing sent.');
    return;
  }

  var res = UrlFetchApp.fetch(ENDPOINT + '?k=' + encodeURIComponent(KEY), {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ rows: rows }),
    muteHttpExceptions: true          // log the failure, don't throw a red script error
  });

  var code = res.getResponseCode();
  Logger.log('AD_SPEND: sent ' + rows.length + ' day(s) ' + start + '..' + today +
             ' -> HTTP ' + code + ' ' + res.getContentText());
  if (code !== 200) {
    throw new Error('AD_SPEND push failed: HTTP ' + code + ' ' + res.getContentText());
  }
}
