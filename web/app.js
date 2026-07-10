/* ===========================================================
   HydroPression Québec — logique applicative
   HYDROPRESSURE_BROWSER_LIVE_2026_07_10

   Principe :
   1) Charger web/data/etat_pression.json comme avant.
   2) Afficher la carte immédiatement avec les dernières données connues.
   3) Tenter ensuite une récupération live directement depuis le navigateur.
   4) Si le navigateur reçoit la couche WFS, mettre les débits à jour à l'écran.
   =========================================================== */

const STATE = {
  data: null,
  mode: "actuelle", // "actuelle" | "etiage"
  map: null,
  markers: new Map(),
  selected: null,
};

const LIVE_WFS_URL =
  "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi" +
  "?service=wfs" +
  "&version=1.1.0" +
  "&request=getfeature" +
  "&typename=stations_igo2_public" +
  "&outputformat=geojson" +
  "&epsg:4326";

/* ---- Couleurs & catégories ---- */
const CATEG = {
  critique:    { color: "#a32424", label: "Critique" },
  eleve:       { color: "#d97a4a", label: "Élevée" },
  modere:      { color: "#e6c14a", label: "Modérée" },
  faible:      { color: "#7eb693", label: "Faible" },
  negligeable: { color: "#1a4a3a", label: "Négligeable" },
  inconnu:     { color: "#b8b3a8", label: "Indéterminée" },
};

const fmt = {
  pct: (v) => v === null || v === undefined ? "—" : `${v.toFixed(1)}\u00a0%`,
  m3s: (v) => v === null || v === undefined ? "—" : Number(v).toFixed(Number(v) < 1 ? 3 : 2),
  km2: (v) => v === null || v === undefined ? "—" : Number(v).toLocaleString("fr-CA", { maximumFractionDigits: 0 }),
  int: (v) => v === null || v === undefined ? "—" : Math.round(Number(v)).toLocaleString("fr-CA"),
  date: (iso) => {
    if (!iso) return "—";
    try {
      let value = String(iso);
      if (!/[zZ]|[+-]\d{2}:\d{2}$/.test(value)) {
        value += "Z";
      }
      const d = new Date(value);
      return d.toLocaleString("fr-CA", {
        dateStyle: "long",
        timeStyle: "short",
        timeZone: "America/Toronto"
      });
    } catch {
      return iso;
    }
  },
};

function normalizeCode(code) {
  if (code === null || code === undefined) return "";
  return String(code).trim().replace(/^0+/, "");
}

function safeNum(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function parseUtc(value) {
  if (!value) return null;
  let txt = String(value).trim();
  if (!/[zZ]|[+-]\d{2}:\d{2}$/.test(txt)) {
    txt += "Z";
  }
  const d = new Date(txt);
  return Number.isNaN(d.getTime()) ? null : d;
}

function categoriser(pressionPct) {
  if (pressionPct === null || pressionPct === undefined || Number.isNaN(pressionPct)) return "inconnu";
  if (pressionPct >= 50) return "critique";
  if (pressionPct >= 30) return "eleve";
  if (pressionPct >= 15) return "modere";
  if (pressionPct >= 5) return "faible";
  return "negligeable";
}

function looksLikeHtml(text) {
  const head = String(text || "").trimStart().slice(0, 1200).toLowerCase();
  return (
    head.includes("<!doctype html") ||
    head.includes("<html") ||
    head.includes("enable javascript") ||
    head.includes("please enable") ||
    head.includes("captcha") ||
    head.includes("challenge") ||
    head.includes("verify that you are not a robot")
  );
}

/* ---- Init ---- */
async function init() {
  try {
    const res = await fetch("data/etat_pression.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    STATE.data = await res.json();
  } catch (e) {
    console.error(e);
    document.getElementById("kpi-meta").textContent = "Données indisponibles. Lancer generate_state.py.";
    return;
  }

  renderFreshnessBanner();
  initKPI();
  initMap();
  renderAll();
  initInteractions();

  // Mise à jour live côté navigateur : non bloquante.
  refreshLiveFromBrowser();
}

/* ---- Live navigateur ---- */
async function refreshLiveFromBrowser() {
  const d = STATE.data;
  if (!d || !Array.isArray(d.stations)) return;

  setKpiMetaMessage("Tentative de mise à jour des débits en temps réel depuis le navigateur…");

  try {
    const live = await fetchLiveGeoJSONFromBrowser();
    const summary = mergeLiveStations(live);

    if (summary.updated > 0) {
      d.n_stations_debit_live = summary.updated;
      d.n_stations_debit_precedent = d.stations.filter(s => s.source_debit_observe === "previous").length;
      d.n_stations_sans_debit_observe = d.stations.filter(s => s.source_debit_observe === "none").length;
      d.latest_live_measure_utc = summary.latestIso;
      d.latest_live_measure_age_hours = summary.ageHours;
      d.data_stale = summary.ageHours !== null && summary.ageHours > (d.stale_threshold_hours ?? 6);
      d.browser_live_ok = true;
      d.browser_live_error = null;
      d.browser_live_fetched_at = new Date().toISOString();

      d.fetch_status = {
        ok: true,
        source: "browser-live-wfs-stations_igo2_public",
        source_url: LIVE_WFS_URL,
        fetched_at: d.browser_live_fetched_at,
        n_features: summary.nFeatures,
        n_avec_debit: summary.updated,
        latest_measure_utc: summary.latestIso,
      };

      rerenderAfterLiveUpdate();

      console.info(`HydroPression live: ${summary.updated} stations mises à jour depuis le navigateur.`);
    } else {
      throw new Error("GeoJSON reçu, mais aucune station avec débit exploitable.");
    }

  } catch (err) {
    const msg = err?.message || String(err);
    console.warn("HydroPression live browser fetch failed:", err);
    d.browser_live_ok = false;
    d.browser_live_error = msg;
    d.data_stale = true;
    renderFreshnessBanner();
    initKPI();
  }
}

async function fetchLiveGeoJSONFromBrowser() {
  const res = await fetch(LIVE_WFS_URL, {
    method: "GET",
    mode: "cors",
    cache: "no-store",
    credentials: "omit",
    headers: {
      "Accept": "application/json, application/geo+json, text/plain, */*"
    }
  });

  const contentType = res.headers.get("content-type") || "";
  const text = await res.text();

  if (!res.ok) {
    throw new Error(`Source live HTTP ${res.status}`);
  }

  if (contentType.toLowerCase().includes("html") || looksLikeHtml(text)) {
    throw new Error(`Source live bloquée par HTML/challenge (${contentType || "Content-Type inconnu"})`);
  }

  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error(`Réponse live non JSON: ${e.message}`);
  }

  if (!data || !Array.isArray(data.features)) {
    throw new Error("GeoJSON live sans tableau features.");
  }

  return data;
}

function mergeLiveStations(geojson) {
  const liveByCode = new Map();
  let latestDate = null;
  let nFeatures = 0;

  for (const f of geojson.features || []) {
    nFeatures += 1;
    const p = f.properties || {};
    const code = normalizeCode(p.station);
    if (!code) continue;

    const debit = safeNum(p.dern_valeur_deb);
    if (debit === null) continue;

    const mesure = p.dern_date_prise_valeur_utc || null;
    const mesureDate = parseUtc(mesure);
    if (mesureDate && (!latestDate || mesureDate > latestDate)) {
      latestDate = mesureDate;
    }

    let lon = null;
    let lat = null;
    const coords = f.geometry?.coordinates;
    if (Array.isArray(coords) && coords.length >= 2) {
      const x = safeNum(coords[0]);
      const y = safeNum(coords[1]);
      if (x !== null && y !== null && x >= -180 && x <= 180 && y >= -90 && y <= 90) {
        lon = x;
        lat = y;
      }
    }

    liveByCode.set(code, {
      debit_obs_m3s: debit,
      niveau_m: safeNum(p.dern_valeur_niv),
      date_mesure: mesure,
      etat: p.etat || null,
      url_cehq: p.fournisseur_url || null,
      lon,
      lat,
    });
  }

  let updated = 0;

  for (const s of STATE.data.stations) {
    const live = liveByCode.get(normalizeCode(s.code));
    if (!live) continue;

    updated += 1;

    s.debit_obs_m3s = live.debit_obs_m3s;
    s.niveau_m = live.niveau_m;
    s.date_mesure = live.date_mesure || s.date_mesure;
    s.etat_cehq = live.etat || s.etat_cehq;
    s.url_cehq = live.url_cehq || s.url_cehq;
    if (live.lon !== null && live.lat !== null) {
      s.lon = live.lon;
      s.lat = live.lat;
    }
    s.source_debit_observe = "browser-live";

    const debitPreleve = safeNum(s.debit_preleve_m3s) ?? 0;
    const debitNaturel = live.debit_obs_m3s + debitPreleve;

    s.debit_naturel_m3s = debitNaturel > 0 ? debitNaturel : null;
    s.pression_observe_pct = debitNaturel > 0 ? (debitPreleve / debitNaturel * 100) : null;
    s.categorie_observe = categoriser(s.pression_observe_pct);
  }

  const latestIso = latestDate ? latestDate.toISOString() : null;
  const ageHours = latestDate ? ((Date.now() - latestDate.getTime()) / 3600000) : null;

  return {
    updated,
    nFeatures,
    latestIso,
    ageHours: ageHours === null ? null : Math.round(ageHours * 10) / 10,
  };
}

function rerenderAfterLiveUpdate() {
  renderFreshnessBanner();
  initKPI();
  refreshMarkers();
  renderTop();

  if (STATE.selected) {
    const current = STATE.data.stations.find(s => s.code === STATE.selected.code);
    if (current) {
      openDetail(current);
    }
  }
}

function setKpiMetaMessage(message) {
  const el = document.getElementById("kpi-meta");
  if (el) el.textContent = message;
}

/* ---- Bandeau de fraîcheur des données ---- */
function renderFreshnessBanner() {
  const d = STATE.data;
  const existing = document.getElementById("freshness-banner");
  if (existing) existing.remove();

  if (!d) return;

  const header = document.querySelector("header.topbar");
  if (!header) return;

  if (d.data_stale !== true) {
    return;
  }

  const latestLive = d.latest_live_measure_utc ? fmt.date(d.latest_live_measure_utc) : null;
  const previousCount = d.n_stations_debit_precedent ?? 0;

  let detail;
  if (d.browser_live_error) {
    detail = `Le navigateur n'a pas réussi à joindre la source temps réel (${d.browser_live_error}). Certaines valeurs affichées proviennent du dernier état connu.`;
  } else if (latestLive) {
    detail = `La mesure live la plus récente date du ${latestLive}; les débits peuvent être périmés.`;
  } else if (previousCount > 0) {
    detail = `La source de débits en temps réel est momentanément indisponible; certaines valeurs affichées proviennent du dernier état connu.`;
  } else {
    detail = `La source de débits en temps réel est momentanément indisponible.`;
  }

  const banner = document.createElement("div");
  banner.id = "freshness-banner";
  banner.className = "freshness-banner";
  banner.setAttribute("role", "alert");
  banner.innerHTML =
    `<span class="freshness-banner__dot" aria-hidden="true"></span>` +
    `<span><strong>Données non à jour.</strong> ${detail}</span>`;
  header.insertAdjacentElement("afterend", banner);
}

/* ---- KPI ---- */
function initKPI() {
  const d = STATE.data;
  document.getElementById("kpi-total").textContent = d.n_stations;
  document.getElementById("kpi-critique").textContent = d.n_critiques_etiage;
  document.getElementById("kpi-eleve").textContent = d.n_eleves_etiage;

  const generated = fmt.date(d.generated_at);
  let meta = `Dernière génération : ${generated}`;

  if (d.browser_live_ok && d.browser_live_fetched_at) {
    meta = `Débits temps réel chargés dans le navigateur : ${fmt.date(d.browser_live_fetched_at)} (${d.n_stations_debit_live ?? 0} stations)`;
  } else if (d.data_stale) {
    meta += " — ⚠️ non à jour";
  }

  document.getElementById("kpi-meta").textContent = meta;
  document.getElementById("foot-updated").textContent = d.browser_live_ok
    ? `Données live chargées le ${fmt.date(d.browser_live_fetched_at)}`
    : `Données générées le ${generated}`;
}

/* ---- Carte ---- */
function initMap() {
  const m = L.map("map", {
    zoomControl: true,
    attributionControl: true,
    minZoom: 5,
    maxZoom: 12,
  }).setView([47.5, -72.5], 6);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://openstreetmap.org">OSM</a>',
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(m);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}.png", {
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(m);

  STATE.map = m;
  refreshMarkers();
}

function refreshMarkers() {
  const m = STATE.map;
  STATE.markers.forEach(mk => m.removeLayer(mk));
  STATE.markers.clear();

  const mode = STATE.mode;
  for (const s of STATE.data.stations) {
    if (s.lat === null || s.lat === undefined || s.lon === null || s.lon === undefined) continue;
    const cat = mode === "actuelle" ? s.categorie_observe : s.categorie_etiage;
    const color = CATEG[cat]?.color ?? CATEG.inconnu.color;

    const mk = L.circleMarker([s.lat, s.lon], {
      radius: 6,
      fillColor: color,
      color: "#f4f1ec",
      weight: 2,
      fillOpacity: 0.92,
      className: "station-marker",
    }).addTo(m);

    mk.bindPopup(buildPopup(s, mode));
    mk.on("click", () => openDetail(s));

    STATE.markers.set(s.code, mk);
  }
}

function buildPopup(s, mode) {
  const pctActuelle = fmt.pct(s.pression_observe_pct);
  const pctEtiage = fmt.pct(s.pression_etiage_pct);
  const moisNom = STATE.data.mois_courant_nom;
  return `
    <div class="popup-name">${s.plan_deau ?? s.nom}</div>
    <div class="popup-sub">Station ${s.code}</div>
    <div class="popup-stat"><span>Pression en ${moisNom}</span><span>${pctActuelle}</span></div>
    <div class="popup-stat"><span>Pression en étiage</span><span>${pctEtiage}</span></div>
    <div class="popup-cta" onclick="openDetailByCode('${s.code}')">→ Voir le détail</div>
  `;
}

window.openDetailByCode = (code) => {
  const s = STATE.data.stations.find(x => x.code === code);
  if (s) openDetail(s);
};

/* ---- TOP 10 ---- */
function renderTop() {
  const list = document.getElementById("top-list");
  const mode = STATE.mode;
  const moisNom = STATE.data.mois_courant_nom;
  const key = mode === "actuelle" ? "pression_observe_pct" : "pression_etiage_pct";
  const sub = mode === "actuelle"
    ? `Pression du mois courant (${moisNom})`
    : "Pression sur le débit Q2,7 d'étiage";
  document.getElementById("top-sub").textContent = sub;
  document.getElementById("map-sub").textContent = mode === "actuelle"
    ? `État en ${moisNom} — pression sur le débit observé`
    : "Risque en étiage — pression sur le débit Q2,7";

  const sorted = [...STATE.data.stations]
    .filter(s => s[key] !== null && s[key] !== undefined && s.n_sites_amont > 0)
    .sort((a, b) => (b[key] ?? 0) - (a[key] ?? 0))
    .slice(0, 10);

  list.innerHTML = "";
  if (sorted.length === 0) {
    list.innerHTML = '<li class="top-empty">Aucune donnée à afficher.</li>';
    return;
  }
  sorted.forEach((s, i) => {
    const cat = mode === "actuelle" ? s.categorie_observe : s.categorie_etiage;
    const li = document.createElement("li");
    li.className = "top-item";
    li.innerHTML = `
      <span class="top-rank">${String(i + 1).padStart(2, "0")}</span>
      <span>
        <div class="top-name">${s.plan_deau ?? s.nom}</div>
        <div class="top-name-sub">Station ${s.code}</div>
      </span>
      <span class="top-pct top-pct--${cat}">${fmt.pct(s[key])}</span>
    `;
    li.onclick = () => {
      openDetail(s);
      if (s.lat && s.lon) STATE.map.flyTo([s.lat, s.lon], 9, { duration: 0.8 });
    };
    list.appendChild(li);
  });
}

/* ---- Détail ---- */
function openDetail(s) {
  STATE.selected = s;
  const moisNom = STATE.data.mois_courant_nom;

  document.getElementById("detail-bv").textContent = s.bv_prim ?? "—";
  document.getElementById("detail-title").textContent = s.plan_deau ?? s.nom;
  const subParts = [
    `Station ${s.code}`,
    s.nom && s.nom !== s.plan_deau ? s.nom : null,
    s.date_mesure ? `Mesure : ${fmt.date(s.date_mesure)}` : null,
  ].filter(Boolean);
  document.getElementById("detail-sub").textContent = subParts.join(" · ");

  const setMetric = (valId, footId, pct, footMsg) => {
    const cat = pct === null ? "inconnu" : categoriser(pct);
    const el = document.getElementById(valId);
    el.textContent = fmt.pct(pct);
    el.style.color = pct === null ? "" : CATEG[cat].color;
    document.getElementById(footId).textContent = footMsg + " · " + (CATEG[cat]?.label ?? "—");
  };

  const labelActuelle = document.querySelector(".metric--big .metric-label");
  if (labelActuelle) {
    labelActuelle.textContent = `État en ${moisNom}`;
  }

  setMetric("m-actuelle", "m-actuelle-foot",
    s.pression_observe_pct, "du débit naturel consommé");
  setMetric("m-etiage", "m-etiage-foot",
    s.pression_etiage_pct, "si la rivière atteignait son Q2,7");

  document.getElementById("m-debit-obs").textContent = fmt.m3s(s.debit_obs_m3s);
  document.getElementById("m-debit-prel").textContent = fmt.m3s(s.debit_preleve_m3s);
  document.getElementById("m-q27").textContent = fmt.m3s(s.q27_ete_m3s);
  document.getElementById("m-sites").textContent = fmt.int(s.n_sites_amont);
  document.getElementById("m-sup").textContent = fmt.km2(s.superficie_km2);

  const footPrel = document.querySelector("#m-debit-prel + .metric-foot");
  if (footPrel) {
    footPrel.textContent = `m³/s — moyenne en ${moisNom} (5 ans)`;
  }

  const lk = document.getElementById("link-cehq");
  if (s.url_cehq) {
    lk.href = s.url_cehq;
    lk.style.display = "";
  } else {
    lk.style.display = "none";
  }

  const tech = document.getElementById("tech-intervenants");
  const intervenantsActifs = (s.intervenants || []).filter(it => {
    const isAggregate = (it.num_site === null || it.num_site === undefined || it.num_site === "");
    return !isAggregate;
  });
  const aggregateRow = (s.intervenants || []).find(it =>
    it.num_site === null || it.num_site === undefined || it.num_site === ""
  );

  if (intervenantsActifs.length > 0) {
    let html = `<p class="tech-intro">Préleveurs avec déclaration en <strong>${moisNom}</strong>, triés par débit consommé moyen :</p>`;
    html += '<table class="tech-table"><thead><tr>';
    html += '<th class="rank-col">#</th>';
    html += '<th>Intervenant</th>';
    html += '<th>Secteur</th>';
    html += '<th>Municipalité</th>';
    html += `<th class="right">Débit consommé<br><span class="th-sub">${moisNom}, m³/s</span></th>`;
    html += '<th class="right">Volume an.<br><span class="th-sub">consommé, Mm³</span></th>';
    html += '<th class="right">Période<br><span class="th-sub">déclarations</span></th>';
    html += '</tr></thead><tbody>';

    intervenantsActifs.forEach((it, idx) => {
      const debit = it.debit_mois_courant_m3s ?? 0;
      const vol = it.volume_annuel_moyen_Mm3;
      const periode = (it.premiere_annee && it.derniere_annee)
        ? `${it.premiere_annee}–${it.derniere_annee}`
        : "—";

      html += `<tr>
        <td class="rank-col mono">${idx + 1}</td>
        <td class="intervenant-cell">${it.nom_intervenant ?? "—"}</td>
        <td>${it.secteur_scian ?? "—"}</td>
        <td>${it.municipalite ?? "—"}</td>
        <td class="right mono">${Number(debit).toFixed(4)}</td>
        <td class="right mono">${vol !== null && vol !== undefined ? Number(vol).toFixed(2) : "—"}</td>
        <td class="right mono small">${periode}</td>
      </tr>`;
    });

    if (aggregateRow && aggregateRow.debit_mois_courant_m3s > 0) {
      const debit = aggregateRow.debit_mois_courant_m3s;
      html += `<tr class="aggregate-row">
        <td class="rank-col mono">…</td>
        <td class="intervenant-cell" colspan="3">${aggregateRow.nom_intervenant}</td>
        <td class="right mono">${Number(debit).toFixed(4)}</td>
        <td class="right mono">—</td>
        <td class="right mono small">—</td>
      </tr>`;
    }

    html += "</tbody></table>";

    if (s.n_sites_inactifs_mois && s.n_sites_inactifs_mois > 0) {
      html += `<p class="tech-note">+ ${s.n_sites_inactifs_mois} préleveur${s.n_sites_inactifs_mois > 1 ? "s" : ""} amont sans déclaration en ${moisNom} (non comptabilisé${s.n_sites_inactifs_mois > 1 ? "s" : ""} dans la pression actuelle).</p>`;
    }

    tech.innerHTML = html;
  } else {
    let html = `<p class="tech-empty">Aucun préleveur avec déclaration en ${moisNom} dans le bassin amont.</p>`;
    if (s.n_sites_amont > 0) {
      html += `<p class="tech-note">${s.n_sites_amont} préleveur${s.n_sites_amont > 1 ? "s" : ""} amont identifié${s.n_sites_amont > 1 ? "s" : ""}, mais aucun n'a déclaré en ${moisNom} sur 2020–2024.</p>`;
    }
    tech.innerHTML = html;
  }

  document.getElementById("detail-tech").hidden = true;
  document.getElementById("toggle-tech").textContent = "+ Vue technique pour analystes";

  document.getElementById("detail").hidden = false;
}

function closeDetail() {
  document.getElementById("detail").hidden = true;
  STATE.selected = null;
}

/* ---- Modes (actuelle / étiage) ---- */
function setMode(mode) {
  STATE.mode = mode;
  document.querySelectorAll(".nav-btn[data-mode]").forEach(b => {
    b.classList.toggle("nav-btn--active", b.dataset.mode === mode);
  });
  refreshMarkers();
  renderTop();
}

/* ---- Render orchestrator ---- */
function renderAll() {
  renderTop();
}

/* ---- Init interactions ---- */
function initInteractions() {
  document.querySelectorAll(".nav-btn[data-mode]").forEach(b => {
    b.addEventListener("click", () => setMode(b.dataset.mode));
  });

  document.getElementById("detail-close").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("detail").hidden) closeDetail();
  });

  document.getElementById("toggle-tech").addEventListener("click", () => {
    const t = document.getElementById("detail-tech");
    const btn = document.getElementById("toggle-tech");
    t.hidden = !t.hidden;
    btn.textContent = t.hidden ? "+ Vue technique pour analystes" : "− Masquer la vue technique";
  });

  document.querySelector("[data-route='apropos']").addEventListener("click", () => {
    location.href = "apropos.html";
  });
}

document.addEventListener("DOMContentLoaded", init);
