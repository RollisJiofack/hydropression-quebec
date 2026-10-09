const { test } = require("node:test");
const assert = require("node:assert/strict");
const { derive } = require("../freshness.js");
const now = Date.parse("2026-10-09T12:00:00Z");
const stamp = hours => new Date(now - hours * 3600000).toISOString();
function snapshot(age = 0.25) {
  return { generated_at: stamp(0.1), latest_live_measure_utc: stamp(age), data_stale: false,
    n_critiques_etiage: 11, n_eleves_etiage: 7,
    stations: [{ code: "040129", date_mesure: stamp(age), debit_obs_m3s: 3,
      debit_naturel_m3s: 4, pression_observe_pct: 25, categorie_observe: "modere",
      pression_etiage_pct: 60, categorie_etiage: "critique" }] };
}
test("recent snapshot preserves current calculation", () => {
  const result = derive(snapshot(), now);
  assert.equal(result.data_stale, false);
  assert.equal(result.stations[0].pression_observe_pct, 25);
});
test("stored false freshness flag cannot hide an expired measurement", () => {
  const result = derive(snapshot(7), now);
  assert.equal(result.data_stale, true);
  assert.equal(result.stations[0].pression_observe_pct, null);
  assert.equal(result.stations[0].debit_obs_dernier_connu_m3s, 3);
  assert.equal(result.n_stations_debit_recent, 0);
});
test("lowflow result and counts survive current expiry", () => {
  const result = derive(snapshot(7), now);
  assert.equal(result.stations[0].pression_etiage_pct, 60);
  assert.equal(result.n_critiques_etiage, 11);
  assert.equal(result.n_eleves_etiage, 7);
});
test("six hour boundary is inclusive", () => {
  assert.equal(derive(snapshot(6), now).stations[0].pression_observe_pct, 25);
  assert.equal(derive(snapshot(6.001), now).stations[0].pression_observe_pct, null);
});
test("future and undated measurements are not current", () => {
  assert.equal(derive(snapshot(-1), now).stations[0].pression_observe_pct, null);
  const data = snapshot();
  data.stations[0].date_mesure = null;
  assert.equal(derive(data, now).stations[0].pression_observe_pct, null);
});
test("derive does not mutate the source snapshot", () => {
  const data = snapshot();
  derive(data, now + 7 * 3600000);
  assert.equal(data.stations[0].pression_observe_pct, 25);
  assert.equal(derive(data, now).stations[0].pression_observe_pct, 25);
});
test("zero is a valid flow; null is not zero", () => {
  const data = snapshot();
  data.stations[0].debit_obs_m3s = 0;
  assert.equal(derive(data, now).n_stations_debit_recent, 1);
  data.stations[0].debit_obs_m3s = null;
  assert.equal(derive(data, now).n_stations_debit_recent, 0);
});
test("timestamps with CEHQ offset compare in UTC", () => {
  const data = snapshot();
  data.stations[0].date_mesure = "2026-10-09T06:45:00-05:00";
  assert.equal(derive(data, now).stations[0].age_mesure_heures, 0.25);
});
