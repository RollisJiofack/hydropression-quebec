#!/usr/bin/env python3
"""
generate_state.py — HydroPression Québec

Génère etat_pression.json consommé par la web app.

Sources :
1. CSV statique :
   - resultats_pression_phase2.csv (12 colonnes mensuelles + étiage)
   - details_intervenants.csv (12 valeurs mensuelles par préleveur)
2. API publique CEHQ : débits actuels.

Logique de calcul de la pression actuelle :
- On détermine le MOIS COURANT (selon la date du jour, fuseau Toronto).
- Pour chaque station, on prend le débit consommé du mois courant.
- On calcule en live :
    pression_actuelle = consommation_mois / (débit_observé_live + consommation_mois) × 100

Logique pour la pression d'étiage :
- Toujours basée sur le débit consommé MAX(juillet, août) calculé statiquement.
- Comparaison au Q2,7 estival.

Usage :
    python generate_state.py [--mois N]   (--mois pour forcer un mois précis, debug)
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from pyproj import Transformer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- Configuration ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "resultats_pression_phase2.csv"
DETAIL_PATH = ROOT / "data" / "details_intervenants.csv"
OUTPUT_PATH = ROOT.parent / "web" / "data" / "etat_pression.json"

API_URL = (
    "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi"
    "?service=wfs&version=1.1.0&request=getfeature"
    "&typename=stations_igo2_public&outputformat=geojson"
)

NOMS_MOIS = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "août",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}


# --- Utilitaires ----------------------------------------------------------

def normalize_code(code) -> str:
    if code is None or pd.isna(code):
        return ""
    return str(code).strip().lstrip("0")


def fetch_stations() -> dict:
    print(f"[{datetime.now():%H:%M:%S}] Appel API CEHQ...")
    t0 = time.time()
    r = requests.get(
        API_URL,
        timeout=60,
        headers={"User-Agent": "HydroPression-Quebec/2.0"},
    )
    r.raise_for_status()
    data = r.json()
    features = data.get("features", [])
    print(f"  {len(features)} stations reçues en {time.time()-t0:.1f}s")
    if not features:
        raise RuntimeError("API CEHQ sans station exploitable")

    transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)

    out = {}
    for f in features:
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


def categoriser(pression_pct):
    if pression_pct is None or pd.isna(pression_pct):
        return "inconnu"
    if pression_pct >= 50: return "critique"
    if pression_pct >= 30: return "eleve"
    if pression_pct >= 15: return "modere"
    if pression_pct >= 5:  return "faible"
    return "negligeable"


def safe_num(v):
    if v is None:
        return None
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def first_num(*values):
    for value in values:
        n = safe_num(value)
        if n is not None:
            return n
    return None


def load_static_results() -> pd.DataFrame:
    if not CSV_PATH.exists():
        print(f"❌ {CSV_PATH} introuvable.")
        sys.exit(1)
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    df["station_norm"] = df["station"].astype(str).apply(normalize_code)
    print(f"  {len(df)} stations dans le CSV de pressions")
    return df


def load_intervenants_detail() -> dict:
    """
    Détails préleveurs avec 12 colonnes mensuelles.
    Retourne un dict {station_code: [list of intervenant dicts]}.
    """
    if not DETAIL_PATH.exists():
        print(f"  ⚠️  Pas de détail intervenants ({DETAIL_PATH.name})")
        return {}
    df = pd.read_csv(DETAIL_PATH, encoding="utf-8-sig")
    df["station_norm"] = df["station"].astype(str).apply(normalize_code)
    out = {}
    for code, group in df.groupby("station_norm"):
        out[code] = group.drop(columns="station_norm").to_dict(orient="records")
    print(f"  Détails intervenants chargés pour {len(out)} stations")
    return out


def load_previous_state() -> dict:
    """
    Charge le dernier JSON publié pour éviter qu'une panne CEHQ remplace
    l'état actuel par des valeurs inconnues.
    """
    if not OUTPUT_PATH.exists():
        return {}
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️  Ancien état illisible ({OUTPUT_PATH.name}) : {e}")
        return {}

    previous = {}
    for station in data.get("stations", []):
        code = normalize_code(station.get("code"))
        if code:
            previous[code] = station
    if previous:
        print(f"  Dernier état chargé pour {len(previous)} stations")
    return previous


def determiner_mois_courant(forcer_mois=None) -> int:
    """Mois courant en fuseau America/Toronto, ou mois forcé pour debug."""
    if forcer_mois:
        return int(forcer_mois)
    return datetime.now(ZoneInfo("America/Toronto")).month


def _clean_for_json(x):
    """Convertit NaN / Inf en None pour produire un JSON valide côté navigateur."""
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        return x

    if isinstance(x, dict):
        return {k: _clean_for_json(v) for k, v in x.items()}

    if isinstance(x, list):
        return [_clean_for_json(v) for v in x]

    return x


# --- Calcul de l'état -----------------------------------------------------

def compute_state(
    static: pd.DataFrame,
    live: dict,
    details: dict,
    mois_courant: int,
    previous: dict | None = None,
) -> dict:
    """
    Combine données statiques (12 mensuels) avec débits live et le mois courant.
    """
    stations_out = []
    n_updated = 0
    n_csv_fallback = 0
    n_previous_fallback = 0
    n_no_debit_obs = 0
    previous = previous or {}

    col_mois = f"debit_preleve_mois_{mois_courant:02d}_m3s"

    for _, row in static.iterrows():
        code = row["station_norm"]
        live_data = live.get(code, {})
        previous_data = previous.get(code, {})

        # Débit observé : live, CSV, puis dernier état connu si CEHQ est muet.
        debit_obs = safe_num(live_data.get("debit_obs_m3s"))
        source_debit_obs = "live" if debit_obs is not None else None
        if debit_obs is not None:
            n_updated += 1
        else:
            debit_obs = safe_num(row.get("debit_obs_m3s"))
            if debit_obs is not None:
                source_debit_obs = "csv"
                n_csv_fallback += 1
            else:
                debit_obs = safe_num(previous_data.get("debit_obs_m3s"))
                if debit_obs is not None:
                    source_debit_obs = "previous"
                    n_previous_fallback += 1
                else:
                    source_debit_obs = "none"
                    n_no_debit_obs += 1

        # Débit consommé du mois courant
        debit_preleve_mois = safe_num(row.get(col_mois)) or 0.0

        # Pression actuelle = consommation_mois / (observé + consommation_mois)
        if debit_obs is not None and debit_preleve_mois is not None:
            debit_naturel = float(debit_obs) + float(debit_preleve_mois)
            pression_obs = (
                (debit_preleve_mois / debit_naturel * 100)
                if debit_naturel > 0 else None
            )
        else:
            debit_naturel = None
            pression_obs = None

        # Pression d'étiage : déjà calculée statiquement
        pression_etiage = safe_num(row.get("pression_etiage_pct"))
        debit_etiage = safe_num(row.get("debit_preleve_etiage_m3s"))
        q27 = safe_num(row.get("q27_ete_m3s"))

        # Construire le tableau des 12 valeurs mensuelles
        debits_mensuels = {}
        for m in range(1, 13):
            v = safe_num(row.get(f"debit_preleve_mois_{m:02d}_m3s"))
            debits_mensuels[f"{m:02d}"] = v if v is not None else 0.0

        # Détails intervenants pour cette station
        intervenants_raw = details.get(code, [])
        intervenants_mois = []
        n_zero = 0
        col_mois_int = f"debit_mois_{mois_courant:02d}_m3s"
        for it in intervenants_raw:
            d = it.get(col_mois_int, 0) or 0
            is_aggregate = (it.get("num_site") is None or pd.isna(it.get("num_site")) or it.get("num_site") == "")
            if d > 0 or is_aggregate:
                it_out = dict(it)
                it_out["debit_mois_courant_m3s"] = float(d) if not pd.isna(d) else 0.0
                for k, v in list(it_out.items()):
                    if isinstance(v, float) and pd.isna(v):
                        it_out[k] = None
                intervenants_mois.append(it_out)
            else:
                n_zero += 1

        intervenants_mois.sort(
            key=lambda x: x.get("debit_mois_courant_m3s", 0) or 0,
            reverse=True
        )

        station_data = {
            "code": str(row["station"]).zfill(6) if str(row["station"]).isdigit() else str(row["station"]),
            "nom": row["nom"],
            "plan_deau": row["plan_deau"],
            "bv_prim": row["bv_prim"],
            "superficie_km2": safe_num(row["superfi_km2"]),
            "n_sites_amont": int(row["n_sites_amont"]) if pd.notna(row["n_sites_amont"]) else 0,
            "n_sites_etiage": int(row["n_sites_etiage"]) if pd.notna(row.get("n_sites_etiage")) else 0,
            "n_sites_inactifs_mois": n_zero,
            "lon": first_num(live_data.get("lon"), row.get("lon"), previous_data.get("lon")),
            "lat": first_num(live_data.get("lat"), row.get("lat"), previous_data.get("lat")),
            "date_mesure": live_data.get("date_mesure") or previous_data.get("date_mesure"),
            "etat_cehq": live_data.get("etat") or previous_data.get("etat_cehq"),
            "url_cehq": live_data.get("url_cehq") or previous_data.get("url_cehq"),
            "source_debit_observe": source_debit_obs,
            "debit_obs_m3s": safe_num(debit_obs),
            "debit_preleve_m3s": safe_num(debit_preleve_mois),
            "debit_naturel_m3s": safe_num(debit_naturel),
            "pression_observe_pct": safe_num(pression_obs),
            "categorie_observe": categoriser(pression_obs),
            "q27_ete_m3s": q27,
            "debit_preleve_etiage_m3s": debit_etiage,
            "pression_etiage_pct": pression_etiage,
            "categorie_etiage": categoriser(pression_etiage),
            "debits_mensuels_m3s": debits_mensuels,
            "intervenants": intervenants_mois,
        }
        stations_out.append(station_data)

    n_critiques = sum(1 for s in stations_out if s["categorie_etiage"] == "critique")
    n_eleves = sum(1 for s in stations_out if s["categorie_etiage"] == "eleve")
    n_localises = sum(1 for s in stations_out if s["lat"] is not None)

    print(
        f"  {n_updated} stations avec débit live, "
        f"{n_csv_fallback} avec valeur CSV, "
        f"{n_previous_fallback} avec dernier débit connu, "
        f"{n_no_debit_obs} sans débit observé"
    )
    print(f"  {n_localises} stations localisées sur la carte")
    print(f"  {n_critiques} en état critique en étiage, {n_eleves} en élevé")

    return {
        "version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mois_courant": mois_courant,
        "mois_courant_nom": NOMS_MOIS[mois_courant],
        "n_stations": len(stations_out),
        "n_stations_debit_live": n_updated,
        "n_stations_debit_csv": n_csv_fallback,
        "n_stations_debit_precedent": n_previous_fallback,
        "n_stations_sans_debit_observe": n_no_debit_obs,
        "n_critiques_etiage": n_critiques,
        "n_eleves_etiage": n_eleves,
        "stations": stations_out,
    }


# --- Main ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mois", type=int, default=None,
                        help="Forcer un mois (1-12) pour debug, sinon mois courant.")
    args = parser.parse_args()

    print("=" * 60)
    print("HydroPression Québec — Génération de l'état")
    print("=" * 60)

    mois_courant = determiner_mois_courant(args.mois)
    print(f"  Mois courant : {mois_courant} ({NOMS_MOIS[mois_courant]})")
    if args.mois:
        print(f"  ⚠️  Mois forcé via --mois (mode debug)")

    static = load_static_results()
    details = load_intervenants_detail()
    previous = load_previous_state()

    try:
        live = fetch_stations()
    except Exception as e:
        print(f"  ⚠️  Échec API CEHQ : {e}")
        print(f"     On continue avec le CSV et le dernier état connu.")
        live = {}

    state = compute_state(static, live, details, mois_courant, previous)
    state = _clean_for_json(state)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\n✅ {OUTPUT_PATH.relative_to(ROOT.parent)} ({size_kb:.1f} KB)")
    print(f"   Mois utilisé : {NOMS_MOIS[mois_courant]}")
    print(f"   Généré à {state['generated_at']}")


if __name__ == "__main__":
    main()
