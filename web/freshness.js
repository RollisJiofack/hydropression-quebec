(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.HydroFreshness = api;
})(typeof window === "undefined" ? this : window, function () {
  function timestamp(value) {
    if (!value) return NaN;
    let text = String(value);
    if (!/[zZ]|[+-]\d{2}:\d{2}$/.test(text)) text += "Z";
    return Date.parse(text);
  }

  function hoursSince(value, now) {
    const time = timestamp(value);
    return Number.isFinite(time) ? (now - time) / 3600000 : null;
  }

  // Recalculate from the original snapshot so a browser left open ages correctly.
  function derive(data, now = Date.now()) {
    const threshold = Number(data.stale_threshold_hours) > 0 ? Number(data.stale_threshold_hours) : 6;
    const stations = (data.stations || []).map(station => {
      const age = hoursSince(station.date_mesure, now);
      const flow = station.debit_obs_m3s;
      const recent = Number.isFinite(flow) && flow >= 0 && age !== null && age >= 0 && age <= threshold;
      return recent ? { ...station, age_mesure_heures: age } : {
        ...station,
        debit_obs_dernier_connu_m3s: station.debit_obs_dernier_connu_m3s ?? flow,
        age_mesure_heures: age,
        debit_obs_m3s: null,
        debit_naturel_m3s: null,
        pression_observe_pct: null,
        categorie_observe: "inconnu",
      };
    });
    const generatedAge = hoursSince(data.generated_at, now);
    const median = data.median_live_measure_utc || data.fetch_status?.median_measure_utc || data.latest_live_measure_utc;
    const medianAge = hoursSince(median, now);
    const recentCount = stations.filter(station => station.debit_obs_m3s !== null).length;
    return {
      ...data, stations,
      n_stations_debit_recent: recentCount,
      n_stations_sans_debit_observe: stations.length - recentCount,
      n_stations_pression_actuelle_calculable: stations.filter(station => station.pression_observe_pct !== null).length,
      data_stale: data.data_stale === true || generatedAge === null || generatedAge < 0 || generatedAge > threshold ||
        medianAge === null || medianAge < 0 || medianAge > threshold || recentCount === 0,
    };
  }

  return { derive, hoursSince };
});
