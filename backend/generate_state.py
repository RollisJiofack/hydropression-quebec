#!/usr/bin/env python3
"""
generate_state.py — HydroPression Québec

Ce script génère le fichier etat_pression.json consommé par la web app.

Sources :
1. CSV statique : resultats_pression_phase2.csv (régénéré annuellement quand
   les déclarations RDPE/RREUE de l'année précédente sont publiées).
2. API publique CEHQ/MSP : débits actuels des stations hydrométriques.
3. CSV optionnel : details_intervenants.csv (vue technique par station).

Sortie : web/data/etat_pression.json

Usage :
    python generate_state.py

Pour automatiser :
    - Cron Linux/Mac : `0 6 * * * cd /chemin && python generate_state.py`
    - Tâche planifiée Windows : créer une tâche pointant vers ce script
    - GitHub Actions : voir .github/workflows/update.yml dans le repo
"""

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd
from pyproj import Transformer

# --- Configuration ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "resultats_pression_phase2.csv"
DETAIL_PATH = ROOT / "data" / "details_intervenants.csv"  # optionnel
OUTPUT_PATH = ROOT.parent / "web" / "data" / "etat_pression.json"

API_URL = (
    "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi"
    "?service=wfs&version=1.1.0&request=getfeature"
    "&typename=stations_igo2_public&outputformat=geojson"
)

# --- Utilitaires -----------------------------------------------------------

def normalize_code(code) -> str:
    """Normaliser un code station pour faire correspondre 030343, 30343 et 30343.0."""
    if code is None:
        return ""
    try:
        if pd.isna(code):
            return ""
    except (TypeError, ValueError):
        pass

    s = str(code).strip()
    if not s:
        return ""

    # Certains fichiers portent des suffixes du type 030343_xyz.
    s = s.split("_")[0].strip()

    # Pandas peut relire un code comme 30343.0.
    if s.endswith(".0"):
        s = s[:-2]

    # Si le code est numérique, retirer les zéros initiaux pour les jointures.
    return s.lstrip("0") or "0"


def display_station_code(code) -> str:
    """Code de station affiché au format 6 chiffres lorsque possible."""
    norm = normalize_code(code)
    return norm.zfill(6) if norm.isdigit() else str(code)


def _safe_num(v):
    """Convertir en float ou None pour le JSON (pas de NaN/Infinity)."""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f) or pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    """Convertir en int ou None."""
    f = _safe_num(v)
    return int(f) if f is not None else None


def _safe_text(v):
    """Convertir en texte ou None, sans propager les NaN."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def clean_for_json(x):
    """Remplace récursivement NaN/Infinity par None pour produire un JSON strict valide."""
    if x is None:
        return None

    if isinstance(x, dict):
        return {k: clean_for_json(v) for k, v in x.items()}

    if isinstance(x, list):
        return [clean_for_json(v) for v in x]

    if isinstance(x, tuple):
        return [clean_for_json(v) for v in x]

    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        return x

    # Types numériques pandas/numpy sans importer numpy directement.
    if type(x).__module__.startswith("numpy"):
        try:
            return clean_for_json(x.item())
        except Exception:
            return None

    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass

    return x


def fetch_stations() -> dict:
    """Récupérer les débits actuels via l'API publique. Retourne {code: dict}."""
    print(f"[{datetime.now():%H:%M:%S}] Appel API CEHQ...")
    t0 = time.time()
    r = requests.get(API_URL, timeout=60)
    r.raise_for_status()
    data = r.json()
    print(f"  {len(data['features'])} stations reçues en {time.time()-t0:.1f}s")

    # L'API renvoie des coordonnées en EPSG:32198, on les convertit en WGS84.
    transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)

    out = {}
    for f in data["features"]:
        p = f["properties"]
        code = normalize_code(p.get("station"))
        if not code:
            continue
        coords = f.get("geometry", {}).get("coordinates")
        lon = lat = None
        if coords and len(coords) >= 2:
            lon, lat = transformer.transform(coords[0], coords[1])
        out[code] = {
            "debit_obs_m3s": p.get("dern_valeur_deb"),
            "niveau_m": p.get("dern_valeur_niv"),
            "date_mesure": p.get("dern_date_prise_valeur_utc"),
            "etat": p.get("etat"),
            "url_cehq": p.get("fournisseur_url"),
            "lon": lon,
            "lat": lat,
        }
    return out


def categoriser(pression_pct: float) -> str:
    """Classifier un pourcentage de pression en niveau d'alerte."""
    if pression_pct is None or pd.isna(pression_pct):
        return "inconnu"
    if pression_pct >= 50:
        return "critique"
    if pression_pct >= 30:
        return "eleve"
    if pression_pct >= 15:
        return "modere"
    if pression_pct >= 5:
        return "faible"
    return "negligeable"


def load_static_results() -> pd.DataFrame:
    """Charger le CSV statique des pressions calculées (Phase 2)."""
    if not CSV_PATH.exists():
        print(f"ERREUR : {CSV_PATH} introuvable.")
        sys.exit(1)
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    if "station" not in df.columns:
        print("ERREUR : resultats_pression_phase2.csv ne contient pas de colonne 'station'.")
        sys.exit(1)
    df["station_norm"] = df["station"].apply(normalize_code)
    print(f"  {len(df)} stations dans le CSV de pressions")
    return df


def load_intervenants_detail() -> dict:
    """
    Charger le détail des intervenants par station pour la vue technique.

    Le rattachement est fait avec un code station normalisé afin que 030343,
    30343 et 30343.0 correspondent à la même station.
    """
    if not DETAIL_PATH.exists():
        print(f"  Pas de détail intervenants ({DETAIL_PATH.name}) — vue technique limitée")
        return {}

    df = pd.read_csv(DETAIL_PATH, encoding="utf-8-sig")
    if df.empty:
        print(f"  {DETAIL_PATH.name} est vide — vue technique limitée")
        return {}
    if "station" not in df.columns:
        print(f"  {DETAIL_PATH.name} ne contient pas de colonne 'station' — vue technique limitée")
        return {}

    df["station_norm"] = df["station"].apply(normalize_code)
    df = df[df["station_norm"] != ""].copy()

    if "rang" in df.columns:
        df["rang_num"] = pd.to_numeric(df["rang"], errors="coerce")
        df = df.sort_values(["station_norm", "rang_num"], na_position="last")

    out = {}
    for code, group in df.groupby("station_norm", sort=False):
        rows = []
        for _, r in group.iterrows():
            rows.append({
                "rang": _safe_int(r.get("rang")),
                "num_site": _safe_text(r.get("num_site")),
                "nom_intervenant": _safe_text(r.get("nom_intervenant")),
                "secteur_scian": _safe_text(r.get("secteur_scian")),
                "municipalite": _safe_text(r.get("municipalite")),
                "debit_estival_m3s": _safe_num(r.get("debit_estival_m3s")),
                "volume_annuel_moyen_Mm3": _safe_num(r.get("volume_annuel_moyen_Mm3")),
                "premiere_annee": _safe_int(r.get("premiere_annee")),
                "derniere_annee": _safe_int(r.get("derniere_annee")),
            })
        out[code] = rows

    print(f"  Détails intervenants chargés : {len(df):,} lignes pour {len(out)} stations")
    return out


# --- Calcul renouvelé en temps réel -----------------------------------------

def compute_state(static: pd.DataFrame, live: dict, details: dict) -> dict:
    """
    Combine les pressions calculées (statiques) avec les débits actuels (live)
    pour produire l'état temps réel.
    """
    stations_out = []
    n_updated = 0
    n_no_live = 0
    n_details_attached = 0

    for _, row in static.iterrows():
        code = normalize_code(row.get("station_norm") or row.get("station"))
        live_data = live.get(code, {})

        # Débit observé : on prend la version live si dispo, sinon la version CSV.
        debit_obs = live_data.get("debit_obs_m3s")
        if debit_obs is None or pd.isna(debit_obs):
            debit_obs = row.get("debit_obs_m3s")
            n_no_live += 1
        else:
            n_updated += 1

        debit_preleve = row.get("debit_preleve_m3s")
        q27 = row.get("q27_ete_m3s")

        # Recalcul pression avec le débit actuel.
        if pd.notna(debit_obs) and pd.notna(debit_preleve):
            debit_naturel = float(debit_obs) + float(debit_preleve)
            pression_obs = (debit_preleve / debit_naturel * 100) if debit_naturel > 0 else None
        else:
            debit_naturel = None
            pression_obs = None

        if pd.notna(q27) and pd.notna(debit_preleve) and (q27 + debit_preleve) > 0:
            pression_etiage = debit_preleve / (q27 + debit_preleve) * 100
        else:
            pression_etiage = None

        intervenants = details.get(code, [])
        if intervenants:
            n_details_attached += 1

        station_data = {
            "code": display_station_code(row.get("station")),
            "nom": _safe_text(row.get("nom")),
            "plan_deau": _safe_text(row.get("plan_deau")) or _safe_text(row.get("nom")),
            "bv_prim": _safe_text(row.get("bv_prim")),
            "superficie_km2": _safe_num(row.get("superfi_km2")),
            "n_sites_amont": _safe_int(row.get("n_sites_amont")) or 0,
            "lon": _safe_num(live_data.get("lon") if live_data.get("lon") is not None else row.get("lon")),
            "lat": _safe_num(live_data.get("lat") if live_data.get("lat") is not None else row.get("lat")),
            "date_mesure": _safe_text(live_data.get("date_mesure")),
            "etat_cehq": _safe_text(live_data.get("etat")),
            "url_cehq": _safe_text(live_data.get("url_cehq")),
            "debit_obs_m3s": _safe_num(debit_obs),
            "debit_preleve_m3s": _safe_num(debit_preleve),
            "debit_naturel_m3s": _safe_num(debit_naturel),
            "q27_ete_m3s": _safe_num(q27),
            "pression_observe_pct": _safe_num(pression_obs),
            "pression_etiage_pct": _safe_num(pression_etiage),
            "categorie_observe": categoriser(pression_obs),
            "categorie_etiage": categoriser(pression_etiage),
            "intervenants": intervenants,
        }
        stations_out.append(station_data)

    # Stats globales.
    n_critiques = sum(1 for s in stations_out if s["categorie_etiage"] == "critique")
    n_eleves = sum(1 for s in stations_out if s["categorie_etiage"] == "eleve")
    n_localises = sum(1 for s in stations_out if s["lat"] is not None)

    print(f"  {n_updated} stations avec débit live, {n_no_live} avec valeur CSV")
    print(f"  {n_localises} stations localisées sur la carte")
    print(f"  {n_critiques} en état critique en étiage, {n_eleves} en élevé")
    print(f"  Détails intervenants rattachés à {n_details_attached} stations")

    return {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_stations": len(stations_out),
        "n_critiques_etiage": n_critiques,
        "n_eleves_etiage": n_eleves,
        "stations": stations_out,
    }


# --- Main ------------------------------------------------------------------

def main():
    print("=" * 60)
    print("HydroPression Québec — Génération de l'état")
    print("=" * 60)

    static = load_static_results()
    details = load_intervenants_detail()

    try:
        live = fetch_stations()
    except Exception as e:
        print(f"  ⚠️  Échec API CEHQ : {e}")
        print("     On continue avec les valeurs du CSV.")
        live = {}

    state = compute_state(static, live, details)
    state = clean_for_json(state)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\n✅ {OUTPUT_PATH.relative_to(ROOT.parent)} ({size_kb:.1f} KB)")
    print(f"   Généré à {state['generated_at']}")


if __name__ == "__main__":
    main()
