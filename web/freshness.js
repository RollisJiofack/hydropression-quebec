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

  // Per-station validity is frozen by Python at generation, not by consultation.
  function derive(data, now = Date.now()) {
    const threshold = Number(data.stale_threshold_hours) > 0 ? Number(data.stale_threshold_hours) : 6;
    const generatedAge = hoursSince(data.generated_at, now);
    const expired = generatedAge === null || generatedAge < 0 || generatedAge > threshold;
    const stations = (data.stations || []).map(station => expired ? {
        ...station,
        debit_obs_dernier_connu_m3s: station.debit_obs_dernier_connu_m3s ?? station.debit_obs_m3s,
        debit_obs_m3s: null,
        debit_naturel_m3s: null,
        pression_observe_pct: null,
        categorie_observe: "inconnu",
      } : { ...station });
    return {
      ...data, stations,
      generation_age_hours: generatedAge,
      generation_expired: expired,
      freshness_warning: !expired && generatedAge >= 2,
      n_stations_debit_recent: expired ? 0 : data.n_stations_debit_recent,
      n_stations_sans_debit_observe: expired ? stations.length : data.n_stations_sans_debit_observe,
      n_stations_pression_actuelle_calculable: expired ? 0 : data.n_stations_pression_actuelle_calculable,
      data_stale: data.data_stale === true || expired,
    };
  }

  return { derive, hoursSince };
});
