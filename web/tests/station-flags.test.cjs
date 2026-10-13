const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const flagsApi = require('../station-flags.js');
const source = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');

function portalContext(data, ok = true) {
  const requested = [];
  const context = vm.createContext({window: {}, document: {addEventListener() {}},
    console: {error() {}}, HydroStationFlags: flagsApi,
    fetch: async url => {
      requested.push(url);
      if (url !== 'data/etat_pression.json') throw new Error('Unexpected second source');
      return {ok, status: ok ? 200 : 503, json: async () => data};
    }});
  vm.runInContext(source, context);
  return {context, requested};
}

test('JSON contains the only warning source, exactly three stations', async () => {
  const reason = 'Rattachement contest\u00e9 : pression \u00e0 confirmer';
  const stations = ['050119', '050135', '050144_16', '040409'].map(code => ({
    code, pression_etiage_pct: 12, categorie_etiage: 'faible',
    signalement_motif: code === '040409' ? null : reason,
    signalement_date: code === '040409' ? null : '2026-10-13',
  }));
  const data = {stations, n_stations: 4};
  const before = JSON.stringify(data);
  const {context, requested} = portalContext(data);
  const output = await vm.runInContext('loadSourceData()', context);
  assert.equal(output, data);
  assert.equal(JSON.stringify(data), before);
  assert.deepEqual(stations.filter(s => flagsApi.warningText(s)).map(s => s.code),
    ['050119', '050135', '050144_16']);
  assert.deepEqual(requested, ['data/etat_pression.json']);
});

test('old JSON loads with no warning fields and no CSV', async () => {
  const data = {stations: [{code: '050119', pression_etiage_pct: 10}], n_stations: 1};
  const {context, requested} = portalContext(data);
  assert.equal(await vm.runInContext('loadSourceData()', context), data);
  assert.equal(flagsApi.warningHtml(data.stations[0]), '');
  assert.deepEqual(requested, ['data/etat_pression.json']);
});

test('absent, null or malformed optional warning never blocks pressures', () => {
  for (const signalement_motif of [undefined, null, '', '  ', 3, {}, []]) {
    const station = {pression_etiage_pct: 28, signalement_motif};
    assert.equal(flagsApi.warningHtml(station), '');
    assert.equal(station.pression_etiage_pct, 28);
  }
});

test('warning HTML escaped, absent for unflagged stations', () => {
  assert.equal(flagsApi.warningHtml({}), '');
  assert.match(flagsApi.warningHtml({signalement_motif: '<script>&"'}), /&lt;script&gt;&amp;&quot;/);
});
test('warning displayed in popup, top list and station panel', () => {
  assert.equal((source.match(/HydroStationFlags.warningHtml\(s\)/g) || []).length, 2);
  assert.match(source, /getElementById\("detail-warning"\)/);
  assert.match(source, /warning.textContent = HydroStationFlags.warningText\(s\)/);
  assert.doesNotMatch(source, /stations_signalees\.csv/);
  assert.equal(fs.existsSync(path.join(__dirname, '../data/stations_signalees.csv')), false);
});

test('new JSON refresh succeeds without any CSV being available', async () => {
  const data = {stations: [{code: '040409', pression_etiage_pct: 28}], generated_at: 'new'};
  const {context, requested} = portalContext(data);
  vm.runInContext('STATE.sourceData = {generated_at: "old"}; refreshView = () => {};', context);
  await vm.runInContext('refreshData()', context);
  assert.equal(vm.runInContext('STATE.sourceData', context), data);
  assert.deepEqual(requested, ['data/etat_pression.json']);
});

test('failed JSON refresh keeps previous valid generation', async () => {
  const {context} = portalContext(null, false);
  vm.runInContext('STATE.sourceData = {generated_at: "old"}; refreshView = () => {};', context);
  await vm.runInContext('refreshData()', context);
  assert.equal(vm.runInContext('STATE.sourceData.generated_at', context), 'old');
  assert.equal(vm.runInContext('STATE.refreshing', context), false);
});

test('JSON without a station array is rejected', async () => {
  const {context} = portalContext({stations: null});
  await assert.rejects(vm.runInContext('loadSourceData()', context), /Liste de stations absente/);
});
