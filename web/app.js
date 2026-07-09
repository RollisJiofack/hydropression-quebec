/* ===========================================================
   HydroPression Québec — logique applicative (v2)
   =========================================================== */

const STATE = {
  data: null,
  mode: "actuelle", // "actuelle" | "etiage"
  map: null,
  markers: new Map(),
  selected: null,
};

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
  m3s: (v) => v === null || v === undefined ? "—" : v.toFixed(v < 1 ? 3 : 2),
  km2: (v) => v === null || v === undefined ? "—" : v.toLocaleString("fr-CA", { maximumFractionDigits: 0 }),
  int: (v) => v === null || v === undefined ? "—" : Math.round(v).toLocaleString("fr-CA"),
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
}

/* ---- Bandeau de fraîcheur des données ---- */
function renderFreshnessBanner() {
  const d = STATE.data;
  const existing = document.getElementById("freshness-banner");
  if (existing) existing.remove();
  // Rétrocompatible : rien si le champ est absent (ancien JSON) ou faux.
  if (!d || d.data_stale !== true) return;

  const header = document.querySelector("header.topbar");
  if (!header) return;

  const latestLive = d.latest_live_measure_utc
    ? fmt.date(d.latest_live_measure_utc)
    : null;

  const previousCount = d.n_stations_debit_precedent ?? 0;

  let detail;
  if (latestLive) {
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
  const staleTag = d.data_stale ? " — ⚠️ non à jour" : "";
  document.getElementById("kpi-meta").textContent = `Dernière mise à jour : ${generated}${staleTag}`;
  document.getElementById("foot-updated").textContent = `Données générées le ${generated}`;
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
    if (s.lat === null || s.lon === null) continue;
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

  // Métriques principales
  const setMetric = (valId, footId, pct, footMsg) => {
    const cat = pct === null ? "inconnu" : (
      pct >= 50 ? "critique" :
      pct >= 30 ? "eleve" :
      pct >= 15 ? "modere" :
      pct >= 5 ? "faible" : "negligeable"
    );
    const el = document.getElementById(valId);
    el.textContent = fmt.pct(pct);
    el.style.color = pct === null ? "" : CATEG[cat].color;
    document.getElementById(footId).textContent = footMsg + " · " + (CATEG[cat]?.label ?? "—");
  };

  // Mise à jour des libellés des cartes principales
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

  // Sous-titre du débit prélevé pour expliciter le mois
  const footPrel = document.querySelector("#m-debit-prel + .metric-foot");
  if (footPrel) {
    footPrel.textContent = `m³/s — moyenne en ${moisNom} (5 ans)`;
  }

  // Lien vers la station CEHQ
  const lk = document.getElementById("link-cehq");
  if (s.url_cehq) {
    lk.href = s.url_cehq;
    lk.style.display = "";
  } else {
    lk.style.display = "none";
  }

  // Intervenants — uniquement ceux avec débit > 0 ce mois
  const tech = document.getElementById("tech-intervenants");
  const intervenantsActifs = (s.intervenants || []).filter(it => {
    const isAggregate = (it.num_site === null || it.num_site === undefined || it.num_site === "");
    return !isAggregate;  // les vrais préleveurs
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
        <td class="right mono">${debit.toFixed(4)}</td>
        <td class="right mono">${vol !== null && vol !== undefined ? vol.toFixed(2) : "—"}</td>
        <td class="right mono small">${periode}</td>
      </tr>`;
    });

    // Ligne agrégée si présente
    if (aggregateRow && aggregateRow.debit_mois_courant_m3s > 0) {
      const debit = aggregateRow.debit_mois_courant_m3s;
      html += `<tr class="aggregate-row">
        <td class="rank-col mono">…</td>
        <td class="intervenant-cell" colspan="3">${aggregateRow.nom_intervenant}</td>
        <td class="right mono">${debit.toFixed(4)}</td>
        <td class="right mono">—</td>
        <td class="right mono small">—</td>
      </tr>`;
    }

    html += "</tbody></table>";

    // Mention des préleveurs sans déclaration au mois courant
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

  // Reset toggle
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
