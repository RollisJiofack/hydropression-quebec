#!/usr/bin/env python3
"""
generate_state.py — HydroPression Québec

Ce script génère le fichier etat_pression.json consommé par la web app.

Sources :
1. CSV statique : resultats_pression_phase2.csv (régénéré annuellement quand
   les déclarations RDPE/RREUE de l'année précédente sont publiées).
2. API publique CEHQ/MSP : débits actuels des stations hydrométriques.

Sortie : web/data/etat_pression.json

Usage :
    python generate_state.py

Pour automatiser :
    - Cron Linux/Mac : `0 6 * * * cd /chemin && python generate_state.py`
    - Tâche planifiée Windows : créer une tâche pointant vers ce script
    - GitHub Actions : voir .github/workflows/update.yml dans le repo
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd
import geopandas as gpd
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
    """Normaliser un code de station : retirer le 0 initial, espaces, etc."""
    if code is None or pd.isna(code):
        return ""
    return str(code).strip().lstrip("0")


def fetch_stations() -> dict:
    """Récupérer les débits actuels via l'API publique. Retourne {code: dict}."""
    print(f"[{datetime.now():%H:%M:%S}] Appel API CEHQ...")
    t0 = time.time()
    r = requests.get(API_URL, timeout=60)
    r.raise_for_status()
    data = r.json()
    print(f"  {len(data['features'])} stations reçues en {time.time()-t0:.1f}s")

    # L'API renvoie des coordonnées en EPSG:32198, on les convertit en WGS84
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
    df["station_norm"] = df["station"].astype(str).apply(normalize_code)
    print(f"  {len(df)} stations dans le CSV de pressions")
    return df


def load_intervenants_detail() -> dict:
    """
    Charger le détail des intervenants par station (pour le niveau 3 d'analyse).
    Format attendu : station, nom_intervenant, debit_jour_m3s, secteur_scian, municipalite
    Optionnel — si le fichier n'existe pas, on retourne un dict vide.
    """
    if not DETAIL_PATH.exists():
        print(f"  Pas de détail intervenants ({DETAIL_PATH.name}) — vue technique limitée")
        return {}
    df = pd.read_csv(DETAIL_PATH, encoding="utf-8-sig")
    df["station_norm"] = df["station"].astype(str).apply(normalize_code)
    out = {}
    for code, group in df.groupby("station_norm"):
        out[code] = group.drop(columns="station_norm").to_dict(orient="records")
    print(f"  Détails intervenants chargés pour {len(out)} stations")
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

    for _, row in static.iterrows():
        code = row["station_norm"]
        live_data = live.get(code, {})

        # Débit observé : on prend la version live si dispo, sinon la version CSV
        debit_obs = live_data.get("debit_obs_m3s")
        if debit_obs is None or pd.isna(debit_obs):
            debit_obs = row["debit_obs_m3s"]
            n_no_live += 1
        else:
            n_updated += 1

        debit_preleve = row["debit_preleve_m3s"]
        q27 = row["q27_ete_m3s"]

        # Recalcul pression avec le débit actuel
        if pd.notna(debit_obs) and pd.notna(debit_preleve):
            debit_naturel = float(debit_obs) + float(debit_preleve)
            pression_obs = (
                (debit_preleve / debit_naturel * 100) if debit_naturel > 0 else None
            )
        else:
            debit_naturel = None
            pression_obs = None

        if pd.notna(q27) and pd.notna(debit_preleve) and (q27 + debit_preleve) > 0:
            pression_etiage = debit_preleve / (q27 + debit_preleve) * 100
        else:
            pression_etiage = None

        station_data = {
            "code": str(row["station"]).zfill(6) if str(row["station"]).isdigit() else str(row["station"]),
            "nom": row["nom"],
            "plan_deau": row["plan_deau"],
            "bv_prim": row["bv_prim"],
            "superficie_km2": _safe_num(row["superfi_km2"]),
            "n_sites_amont": int(row["n_sites_amont"]) if pd.notna(row["n_sites_amont"]) else 0,
            "lon": _safe_num(live_data.get("lon") or row.get("lon")),
            "lat": _safe_num(live_data.get("lat") or row.get("lat")),
            "date_mesure": live_data.get("date_mesure"),
            "etat_cehq": live_data.get("etat"),
            "url_cehq": live_data.get("url_cehq"),
            "debit_obs_m3s": _safe_num(debit_obs),
            "debit_preleve_m3s": _safe_num(debit_preleve),
            "debit_naturel_m3s": _safe_num(debit_naturel),
            "q27_ete_m3s": _safe_num(q27),
            "pression_observe_pct": _safe_num(pression_obs),
            "pression_etiage_pct": _safe_num(pression_etiage),
            "categorie_observe": categoriser(pression_obs),
            "categorie_etiage": categoriser(pression_etiage),
            "intervenants": details.get(code, []),
        }
        stations_out.append(station_data)

    # Stats globales
    n_critiques = sum(1 for s in stations_out if s["categorie_etiage"] == "critique")
    n_eleves = sum(1 for s in stations_out if s["categorie_etiage"] == "eleve")
    n_localises = sum(1 for s in stations_out if s["lat"] is not None)

    print(f"  {n_updated} stations avec débit live, {n_no_live} avec valeur CSV")
    print(f"  {n_localises} stations localisées sur la carte")
    print(f"  {n_critiques} en état critique en étiage, {n_eleves} en élevé")

    return {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_stations": len(stations_out),
        "n_critiques_etiage": n_critiques,
        "n_eleves_etiage": n_eleves,
        "stations": stations_out,
    }


def _safe_num(v):
    """Convertir en float ou None pour le JSON (pas de NaN qui plante en JS)."""
    if v is None:
        return None
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


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
        print(f"     On continue avec les valeurs du CSV.")
        live = {}

    state = compute_state(static, live, details)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\n✅ {OUTPUT_PATH.relative_to(ROOT.parent)} ({size_kb:.1f} KB)")
    print(f"   Généré à {state['generated_at']}")


if __name__ == "__main__":
    main()
