/* Mapping warnings are display metadata, never pressure inputs. */
(function (root) {
  function warningText(station) {
    return typeof station?.signalement_motif === "string" ? station.signalement_motif.trim() : "";
  }
  function warningHtml(station) {
    const reason = warningText(station);
    if (!reason) return "";
    const escaped = reason.replace(/[&<>"']/g, c =>
      ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'})[c]);
    return `<p class="station-warning">${escaped}</p>`;
  }
  const api = { warningText, warningHtml };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HydroStationFlags = api;
})(typeof window !== "undefined" ? window : this);
