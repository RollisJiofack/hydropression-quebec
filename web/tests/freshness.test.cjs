const { test } = require("node:test");
const assert = require("node:assert/strict");
const { derive, hoursSince } = require("../freshness.js");
const now = Date.parse("2026-10-09T12:00:00Z");
const stamp = hours => new Date(now - hours * 3600000).toISOString();
function snapshot(age = 0.25, flow = 3, dated = true) {
  const recent = flow !== null && dated && age >= 0 && age <= 6;
  return { generated_at: stamp(0), latest_live_measure_utc: stamp(age), data_stale: !recent,
    n_stations_debit_recent: recent ? 1 : 0, n_stations_sans_debit_observe: recent ? 0 : 1,
    n_stations_pression_actuelle_calculable: recent ? 1 : 0,
    n_critiques_etiage: 11, n_eleves_etiage: 7,
    stations: [{ code: "040129", date_mesure: dated ? stamp(age) : null, debit_obs_m3s: recent ? flow : null,
      debit_obs_dernier_connu_m3s: flow, age_mesure_heures: age,
      statut_debit_observe: recent ? "recent" : "perime",
      debit_naturel_m3s: recent ? flow + 1 : null,
      pression_observe_pct: recent ? 100 / (flow + 1) : null,
      categorie_observe: recent ? "modere" : "inconnu",
      pression_etiage_pct: 60, categorie_etiage: "critique" }] };
}
test("recent snapshot preserves current calculation", () => {
  const result = derive(snapshot(), now);
  assert.equal(result.data_stale, false);
  assert.equal(result.stations[0].pression_observe_pct, 25);
});
test("measurement expired at generation stays excluded from the JSON snapshot", () => {
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
test("future and undated measurements excluded by the producer remain unavailable", () => {
  assert.equal(derive(snapshot(-1), now).stations[0].pression_observe_pct, null);
  const data = snapshot(0.25, 3, false);
  assert.equal(derive(data, now).stations[0].pression_observe_pct, null);
});
test("derive does not mutate the source snapshot", () => {
  const data = snapshot();
  derive(data, now + 7 * 3600000);
  assert.equal(data.stations[0].pression_observe_pct, 25);
  assert.equal(derive(data, now).stations[0].pression_observe_pct, 25);
});
test("zero is a valid flow; null is not zero", () => {
  const data = snapshot(0.25, 0);
  assert.equal(derive(data, now).n_stations_debit_recent, 1);
  assert.equal(derive(snapshot(0.25, null), now).n_stations_debit_recent, 0);
});
test("timestamps with CEHQ offset compare in UTC without changing station validity", () => {
  assert.equal(hoursSince("2026-10-09T06:45:00-05:00", now), 0.25);
});

test("same generation keeps identical counts and pressures at plus one and five hours", () => {
  const data = snapshot(5 + 59 / 60);
  for (const hours of [1, 5]) {
    const value = derive(data, now + hours * 3600000);
    assert.equal(value.n_stations_debit_recent, 1);
    assert.equal(value.n_stations_pression_actuelle_calculable, 1);
    assert.equal(value.stations[0].pression_observe_pct, 25);
    assert.equal(value.stations[0].age_mesure_heures, data.stations[0].age_mesure_heures);
  }
});

test("generation is valid through six hours and expires at six hours one minute", () => {
  const data = snapshot();
  assert.equal(derive(data, now + 6 * 3600000).stations[0].pression_observe_pct, 25);
  const expired = derive(data, now + (6 * 60 + 1) * 60000);
  assert.equal(expired.stations[0].pression_observe_pct, null);
  assert.equal(expired.n_stations_debit_recent, 0);
  assert.equal(expired.stations[0].pression_etiage_pct, 60);
});

test("generation warning starts at exactly two hours", () => {
  assert.equal(derive(snapshot(), now + 119 * 60000).freshness_warning, false);
  assert.equal(derive(snapshot(), now + 120 * 60000).freshness_warning, true);
});

test("missing or future generation hides current pressure globally", () => {
  for (const generated_at of [null, "invalid", new Date(now + 1).toISOString()]) {
    assert.equal(derive({ ...snapshot(), generated_at }, now).stations[0].pression_observe_pct, null);
  }
});
