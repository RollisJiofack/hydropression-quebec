/* ===========================================================
   HydroPression Québec — logique applicative (corrigée)
   =========================================================== */

const STATE = {
  data: null,
  mode: "actuelle",
  map: null,
  markers: new Map(),
  selected: null,
};

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
      const d = new Date(iso);
      return d.toLocaleString("fr-CA", { dateStyle: "long", timeStyle: "short" });
    } catch { return iso; }
  },
};

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

  initKPI();
  initMap();
  renderAll();
  initInteractions();
}

function initKPI() {
  const d = STATE.data;
  document.getElementById("kpi-total").textContent = d.n_stations;
  document.getElementById("kpi-critique").textContent = d.n_critiques_etiage;
  document.getElementById("kpi-eleve").textContent = d.n_eleves_etiage;
  const generated = fmt.date(d.generated_at);
  document.getElementById("kpi-meta").textContent = `Dernière mise à jour : ${generated}`;
  document.getElementById("foot-updated").textContent = `Données générées le ${generated}`;
}

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
    const pct = mode === "actuelle" ? s.pression_observe_pct : s.pression_etiage_pct;
    const color = CATEG[cat]?.color ?? CATEG.inconnu.color;
    const r = 6;

    const mk = L.circleMarker([s.lat, s.lon], {
      radius: r,
      fillColor: color,
      color: "#f4f1ec",
      weight: 2,
      fillOpacity: 0.92,
      className: "station-marker",
      bubblingMouseEvents: false,
    }).addTo(m);

    mk.bindTooltip(s.plan_deau ?? s.nom, { direction: "top", offset: [0, -6], sticky: true });

    mk.on("click", function(e) {
      L.DomEvent.stopPropagation(e);
      openDetail(s);
    });

    STATE.markers.set(s.code, mk);
  }
}

function renderTop() {
  const list = document.getElementById("top-list");
  const mode = STATE.mode;
  const key = mode === "actuelle" ? "pression_observe_pct" : "pression_etiage_pct";
  const sub = mode === "actuelle"
    ? "Pression sur le débit actuel"
    : "Pression sur le débit Q2,7 d'étiage";
  document.getElementById("top-sub").textContent = sub;
  document.getElementById("map-sub").textContent = mode === "actuelle"
    ? "État actuel — pression sur le débit observé"
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
      if (s.lat && s.lon) {
        STATE.map.setView([s.lat, s.lon], 9, { animate: false });
      }
    };
    list.appendChild(li);
  });
}

function openDetail(s) {
  STATE.selected = s;

  document.getElementById("detail-bv").textContent = s.bv_prim ?? "—";
  document.getElementById("detail-title").textContent = s.plan_deau ?? s.nom;
  const subParts = [
    `Station ${s.code}`,
    s.nom && s.nom !== s.plan_deau ? s.nom : null,
    s.date_mesure ? `Mesure : ${fmt.date(s.date_mesure)}` : null,
  ].filter(Boolean);
  document.getElementById("detail-sub").textContent = subParts.join(" · ");

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
  setMetric("m-actuelle", "m-actuelle-foot", s.pression_observe_pct, "du débit naturel ponctionné");
  setMetric("m-etiage", "m-etiage-foot", s.pression_etiage_pct, "si la rivière atteignait son Q2,7");

  document.getElementById("m-debit-obs").textContent = fmt.m3s(s.debit_obs_m3s);
  document.getElementById("m-debit-prel").textContent = fmt.m3s(s.debit_preleve_m3s);
  document.getElementById("m-q27").textContent = fmt.m3s(s.q27_ete_m3s);
  document.getElementById("m-sites").textContent = fmt.int(s.n_sites_amont);
  document.getElementById("m-sup").textContent = fmt.km2(s.superficie_km2);

  const p = s.debit_preleve_m3s;
  const o = s.debit_obs_m3s;
  const q = s.q27_ete_m3s;
  document.getElementById("f-prel").textContent = fmt.m3s(p);
  document.getElementById("f-prel2").textContent = fmt.m3s(p);
  document.getElementById("f-prel3").textContent = fmt.m3s(p);
  document.getElementById("f-prel4").textContent = fmt.m3s(p);
  document.getElementById("f-obs").textContent = fmt.m3s(o);
  document.getElementById("f-q27").textContent = fmt.m3s(q);
  document.getElementById("f-result").textContent = fmt.pct(s.pression_observe_pct);
  document.getElementById("f-result-q").textContent = fmt.pct(s.pression_etiage_pct);

  const lk = document.getElementById("link-cehq");
  if (s.url_cehq) {
    lk.href = s.url_cehq;
    lk.style.display = "";
  } else {
    lk.style.display = "none";
  }

  const tech = document.getElementById("tech-intervenants");
  if (s.intervenants && s.intervenants.length > 0) {
    let html = '<table class="tech-table"><thead><tr><th>Intervenant</th><th>Secteur</th><th class="right">Débit (m³/s)</th></tr></thead><tbody>';
    for (const it of s.intervenants.slice(0, 15)) {
      html += `<tr>
        <td>${it.nom_intervenant ?? "—"}</td>
        <td>${it.secteur_scian ?? "—"}</td>
        <td class="right mono">${(it.debit_jour_m3s ?? 0).toFixed(4)}</td>
      </tr>`;
    }
    html += "</tbody></table>";
    tech.innerHTML = html;
  } else {
    tech.innerHTML = '<p class="tech-empty">Le détail des intervenants n\'est pas inclus dans cette version pilote. Pour l\'analyse interne, croiser le bassin avec le fichier RDPE.</p>';
  }

  document.getElementById("detail-tech").hidden = true;
  document.getElementById("toggle-tech").textContent = "+ Vue technique pour analystes";

  document.getElementById("detail").hidden = false;
}

function closeDetail() {
  document.getElementById("detail").hidden = true;
  STATE.selected = null;
}

function setMode(mode) {
  STATE.mode = mode;
  document.querySelectorAll(".nav-btn[data-mode]").forEach(b => {
    b.classList.toggle("nav-btn--active", b.dataset.mode === mode);
  });
  refreshMarkers();
  renderTop();
}

function renderAll() {
  renderTop();
}

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
