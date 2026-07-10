#!/usr/bin/env python3
# HYDROPRESSURE_CLEAN_WFS_2026_07_10
"""
generate_state.py — HydroPression Québec

Génère web/data/etat_pression.json consommé par la web app.

Version propre 2026-07-10 :
- AUCUN navigateur automatisé
- AUCUN repli navigateur
- AUCUN Edge / Chromium
- Source officielle Données Québec / MSP Vigilance :
  stations_igo2_public via WFS GeoJSON
- Repli CSV WFS si le GeoJSON échoue
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from pyproj import Transformer


# --- Console ---------------------------------------------------------------

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# --- Configuration ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "resultats_pression_phase2.csv"
DETAIL_PATH = ROOT / "data" / "details_intervenants.csv"
OUTPUT_PATH = ROOT.parent / "web" / "data" / "etat_pression.json"

# URL officielle donnée dans la ressource Données Québec.
# Le paramètre epsg:4326 est conservé tel quel, comme dans l'URL source.
WFS_BASE_URL = "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi"

WFS_COMMON_PARAMS = {
    "service": "wfs",
    "version": "1.1.0",
    "request": "getfeature",
    "typename": "stations_igo2_public",
    "epsg:4326": "",
}

STALE_AFTER_HOURS = float(os.environ.get("HP_STALE_AFTER_HOURS", "6"))
FETCH_RETRIES = int(os.environ.get("HP_FETCH_RETRIES", "3"))
FETCH_TIMEOUT = int(os.environ.get("HP_FETCH_TIMEOUT", "60"))

NOMS_MOIS = {
    1: "janvier",
    2: "février",
    3: "mars",
    4: "avril",
    5: "mai",
    6: "juin",
    7: "juillet",
    8: "août",
    9: "septembre",
    10: "octobre",
    11: "novembre",
    12: "décembre",
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, application/geo+json, text/plain, */*",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


class ChallengeDetected(RuntimeError):
    """Réponse HTML/challenge au lieu d'une donnée exploitable."""


# --- Utilitaires -----------------------------------------------------------

def normalize_code(code) -> str:
    if code is None or pd.isna(code):
        return ""
    return str(code).strip().lstrip("0")


def safe_num(value):
    if value is None:
        return None
    try:
        number = float(value)
        if pd.isna(number) or math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def first_num(*values):
    for value in values:
        number = safe_num(value)
        if number is not None:
            return number
    return None


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:1200].lower()
    signals = (
        "<!doctype html",
        "<html",
        "enable javascript",
        "please enable",
        "captcha",
        "incapsula",
        "cloudflare",
        "challenge",
        "verify you are human",
        "verify that you are not a robot",
        "robot",
    )
    return any(signal in head for signal in signals)


def _parse_utc(value):
    if not value:
        return None

    txt = str(value).strip()

    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    base = txt.replace("Z", "").split("+")[0].split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(base, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def _clean_for_json(value):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, dict):
        return {key: _clean_for_json(item) for key, item in value.items()}

    if isinstance(value, list):
        return [_clean_for_json(item) for item in value]

    return value


def _wfs_url(output_format: str) -> str:
    params = dict(WFS_COMMON_PARAMS)
    params["outputformat"] = output_format

    prepared = requests.Request("GET", WFS_BASE_URL, params=params).prepare()
    return prepared.url


def _extract_coord_pair(geometry):
    if not geometry:
        return None, None

    coords = geometry.get("coordinates")
    if not coords:
        return None, None

    # Point attendu : [x, y]. Repli pour coordonnées imbriquées.
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]

    if not isinstance(coords, list) or len(coords) < 2:
        return None, None

    return safe_num(coords[0]), safe_num(coords[1])


def _extract_lon_lat_from_wkt(value):
    if value is None:
        return None, None

    txt = str(value)
    match = re.search(r"POINT\s*\(\s*([\-0-9.]+)\s+([\-0-9.]+)\s*\)", txt, re.I)
    if not match:
        return None, None

    return safe_num(match.group(1)), safe_num(match.group(2))


def _to_lon_lat(x, y, transformer_32198_to_4326):
    if x is None or y is None:
        return None, None

    # Si déjà en longitude/latitude.
    if -180 <= x <= 180 and -90 <= y <= 90:
        return x, y

    try:
        lon, lat = transformer_32198_to_4326.transform(x, y)
        return safe_num(lon), safe_num(lat)
    except Exception:
        return None, None


def categoriser(pression_pct):
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


# --- Fetch live data -------------------------------------------------------

def _request_text(url: str, accept: str) -> tuple[str | None, dict]:
    meta = {
        "ok": False,
        "error": None,
        "status_code": None,
        "content_type": None,
        "url": url,
    }

    session = requests.Session()
    headers = dict(REQUEST_HEADERS)
    headers["Accept"] = accept

    last_error = None

    for attempt in range(1, FETCH_RETRIES + 1):
        print(f"[{datetime.now():%H:%M:%S}] Appel source live ({attempt}/{FETCH_RETRIES})")
        print(f"  URL : {url}")

        try:
            response = session.get(url, headers=headers, timeout=FETCH_TIMEOUT)
            meta["status_code"] = response.status_code
            meta["content_type"] = response.headers.get("Content-Type", "")
            response.raise_for_status()

            body = response.text
            content_type = (response.headers.get("Content-Type") or "").lower()

            if "html" in content_type or _looks_like_html(body):
                raise ChallengeDetected(
                    f"réponse HTML/challenge (Content-Type={content_type or 'inconnu'})"
                )

            meta["ok"] = True
            return body, meta

        except (requests.RequestException, ChallengeDetected) as exc:
            last_error = exc
            print(f"  ⚠️ Échec : {exc}")

            if attempt < FETCH_RETRIES:
                wait_s = 2 ** attempt
                print(f"     Nouvelle tentative dans {wait_s}s...")
                time.sleep(wait_s)

    meta["error"] = f"{type(last_error).__name__}: {last_error}"
    return None, meta


def _live_from_geojson() -> tuple[dict, dict]:
    url = _wfs_url("geojson")
    body, meta = _request_text(url, "application/json, application/geo+json, */*")

    meta.update({
        "format": "geojson",
        "n_features": 0,
        "n_avec_debit": 0,
        "latest_measure_utc": None,
        "source": "donneesquebec-wfs-stations_igo2_public-geojson",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })

    if not meta.get("ok") or body is None:
        return {}, meta

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        meta["ok"] = False
        meta["error"] = f"JSONDecodeError: {exc}"
        return {}, meta

    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        meta["ok"] = False
        meta["error"] = "GeoJSON sans liste 'features'"
        return {}, meta

    transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)

    out = {}
    latest = None
    n_avec_debit = 0

    for feature in features:
        props = feature.get("properties") or {}
        code = normalize_code(props.get("station"))
        if not code:
            continue

        x, y = _extract_coord_pair(feature.get("geometry"))
        lon, lat = _to_lon_lat(x, y, transformer)

        debit = safe_num(props.get("dern_valeur_deb"))
        if debit is not None:
            n_avec_debit += 1

        date_mesure = props.get("dern_date_prise_valeur_utc")
        dt = _parse_utc(date_mesure)
        if dt is not None and (latest is None or dt > latest):
            latest = dt

        out[code] = {
            "debit_obs_m3s": debit,
            "niveau_m": safe_num(props.get("dern_valeur_niv")),
            "date_mesure": date_mesure,
            "etat": props.get("etat"),
            "url_cehq": props.get("fournisseur_url"),
            "lon": lon,
            "lat": lat,
        }

    meta["n_features"] = len(features)
    meta["n_avec_debit"] = n_avec_debit
    meta["latest_measure_utc"] = latest.isoformat() if latest else None

    print(f"  GeoJSON : {len(out)} stations, {n_avec_debit} avec débit")

    return out, meta


def _live_from_csv() -> tuple[dict, dict]:
    url = _wfs_url("csv")
    body, meta = _request_text(url, "text/csv, text/plain, */*")

    meta.update({
        "format": "csv",
        "n_features": 0,
        "n_avec_debit": 0,
        "latest_measure_utc": None,
        "source": "donneesquebec-wfs-stations_igo2_public-csv",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })

    if not meta.get("ok") or body is None:
        return {}, meta

    try:
        df = pd.read_csv(io.StringIO(body), sep=None, engine="python")
    except Exception as exc:
        meta["ok"] = False
        meta["error"] = f"CSVReadError: {exc}"
        return {}, meta

    if "station" not in df.columns:
        meta["ok"] = False
        meta["error"] = f"CSV sans colonne station. Colonnes: {list(df.columns)}"
        return {}, meta

    transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)

    out = {}
    latest = None
    n_avec_debit = 0

    for _, row in df.iterrows():
        code = normalize_code(row.get("station"))
        if not code:
            continue

        lon = lat = None
        for geom_col in ("geometry", "geom", "the_geom", "wkt"):
            if geom_col in row:
                x, y = _extract_lon_lat_from_wkt(row.get(geom_col))
                lon, lat = _to_lon_lat(x, y, transformer)
                if lon is not None and lat is not None:
                    break

        debit = safe_num(row.get("dern_valeur_deb"))
        if debit is not None:
            n_avec_debit += 1

        date_mesure = row.get("dern_date_prise_valeur_utc")
        dt = _parse_utc(date_mesure)
        if dt is not None and (latest is None or dt > latest):
            latest = dt

        out[code] = {
            "debit_obs_m3s": debit,
            "niveau_m": safe_num(row.get("dern_valeur_niv")),
            "date_mesure": date_mesure,
            "etat": row.get("etat"),
            "url_cehq": row.get("fournisseur_url"),
            "lon": lon,
            "lat": lat,
        }

    meta["n_features"] = len(df)
    meta["n_avec_debit"] = n_avec_debit
    meta["latest_measure_utc"] = latest.isoformat() if latest else None

    print(f"  CSV : {len(out)} stations, {n_avec_debit} avec débit")

    return out, meta


def fetch_stations() -> tuple[dict, dict]:
    live, meta = _live_from_geojson()
    if meta.get("ok") and live:
        return live, meta

    geojson_error = meta.get("error")
    print(f"  ⚠️ Repli CSV après échec GeoJSON : {geojson_error}")

    live_csv, meta_csv = _live_from_csv()
    if meta_csv.get("ok") and live_csv:
        meta_csv["geojson_error"] = geojson_error
        return live_csv, meta_csv

    meta_csv["geojson_error"] = geojson_error
    return {}, meta_csv


# --- Static files ----------------------------------------------------------

def load_static_results() -> pd.DataFrame:
    if not CSV_PATH.exists():
        print(f"❌ {CSV_PATH} introuvable.")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    df["station_norm"] = df["station"].astype(str).apply(normalize_code)

    print(f"  {len(df)} stations dans le CSV de pressions")
    return df


def load_intervenants_detail() -> dict:
    if not DETAIL_PATH.exists():
        print(f"  ⚠️ Pas de détail intervenants ({DETAIL_PATH.name})")
        return {}

    df = pd.read_csv(DETAIL_PATH, encoding="utf-8-sig")
    df["station_norm"] = df["station"].astype(str).apply(normalize_code)

    out = {}
    for code, group in df.groupby("station_norm"):
        out[code] = group.drop(columns="station_norm").to_dict(orient="records")

    print(f"  Détails intervenants chargés pour {len(out)} stations")
    return out


def load_previous_state() -> dict:
    if not OUTPUT_PATH.exists():
        return {}

    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ⚠️ Ancien état illisible ({OUTPUT_PATH.name}) : {exc}")
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
    if forcer_mois:
        return int(forcer_mois)
    return datetime.now(ZoneInfo("America/Toronto")).month


# --- State computation -----------------------------------------------------

def compute_state(
    static: pd.DataFrame,
    live: dict,
    details: dict,
    mois_courant: int,
    previous: dict | None = None,
    fetch_status: dict | None = None,
) -> dict:
    stations_out = []
    n_updated = 0
    n_csv_fallback = 0
    n_previous_fallback = 0
    n_no_debit_obs = 0

    previous = previous or {}
    fetch_status = fetch_status or {}

    col_mois = f"debit_preleve_mois_{mois_courant:02d}_m3s"

    for _, row in static.iterrows():
        code = row["station_norm"]
        live_data = live.get(code, {})
        previous_data = previous.get(code, {})

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

        debit_preleve_mois = safe_num(row.get(col_mois)) or 0.0

        if debit_obs is not None:
            debit_naturel = float(debit_obs) + float(debit_preleve_mois)
            pression_obs = (
                debit_preleve_mois / debit_naturel * 100
                if debit_naturel > 0
                else None
            )
        else:
            debit_naturel = None
            pression_obs = None

        pression_etiage = safe_num(row.get("pression_etiage_pct"))
        debit_etiage = safe_num(row.get("debit_preleve_etiage_m3s"))
        q27 = safe_num(row.get("q27_ete_m3s"))

        debits_mensuels = {}
        for mois in range(1, 13):
            value = safe_num(row.get(f"debit_preleve_mois_{mois:02d}_m3s"))
            debits_mensuels[f"{mois:02d}"] = value if value is not None else 0.0

        intervenants_raw = details.get(code, [])
        intervenants_mois = []
        n_zero = 0
        col_mois_int = f"debit_mois_{mois_courant:02d}_m3s"

        for item in intervenants_raw:
            d = item.get(col_mois_int, 0) or 0
            is_aggregate = (
                item.get("num_site") is None
                or pd.isna(item.get("num_site"))
                or item.get("num_site") == ""
            )

            if d > 0 or is_aggregate:
                item_out = dict(item)
                item_out["debit_mois_courant_m3s"] = (
                    float(d) if not pd.isna(d) else 0.0
                )

                for key, value in list(item_out.items()):
                    if isinstance(value, float) and pd.isna(value):
                        item_out[key] = None

                intervenants_mois.append(item_out)
            else:
                n_zero += 1

        intervenants_mois.sort(
            key=lambda item: item.get("debit_mois_courant_m3s", 0) or 0,
            reverse=True,
        )

        station_data = {
            "code": (
                str(row["station"]).zfill(6)
                if str(row["station"]).isdigit()
                else str(row["station"])
            ),
            "nom": row["nom"],
            "plan_deau": row["plan_deau"],
            "bv_prim": row["bv_prim"],
            "superficie_km2": safe_num(row["superfi_km2"]),
            "n_sites_amont": (
                int(row["n_sites_amont"]) if pd.notna(row["n_sites_amont"]) else 0
            ),
            "n_sites_etiage": (
                int(row["n_sites_etiage"]) if pd.notna(row.get("n_sites_etiage")) else 0
            ),
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

    n_critiques = sum(1 for item in stations_out if item["categorie_etiage"] == "critique")
    n_eleves = sum(1 for item in stations_out if item["categorie_etiage"] == "eleve")
    n_localises = sum(1 for item in stations_out if item["lat"] is not None)

    print(
        f"  {n_updated} stations avec débit live, "
        f"{n_csv_fallback} avec valeur CSV, "
        f"{n_previous_fallback} avec dernier débit connu, "
        f"{n_no_debit_obs} sans débit observé"
    )
    print(f"  {n_localises} stations localisées sur la carte")
    print(f"  {n_critiques} en état critique en étiage, {n_eleves} en élevé")

    latest_iso = fetch_status.get("latest_measure_utc")
    latest_dt = _parse_utc(latest_iso)
    age_hours = None

    if latest_dt is not None:
        age_hours = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600.0

    data_stale = (
        not fetch_status.get("ok", False)
        or n_updated == 0
        or (age_hours is not None and age_hours > STALE_AFTER_HOURS)
    )

    if data_stale:
        reason = (
            fetch_status.get("error")
            or ("aucune station avec débit live" if n_updated == 0 else None)
            or (
                f"source périmée : dernière mesure il y a {age_hours:.1f} h "
                f"(seuil {STALE_AFTER_HOURS} h)"
                if age_hours is not None
                else "cause inconnue"
            )
        )
        print(f"  🔴 DONNÉES NON À JOUR — {reason}")
    else:
        age_txt = f"{age_hours:.1f} h" if age_hours is not None else "inconnu"
        print(f"  🟢 Données à jour (dernière mesure il y a {age_txt})")

    return {
        "version": "2.2-clean-wfs",
        "generator": "HYDROPRESSURE_CLEAN_WFS_2026_07_10",
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
        "data_stale": bool(data_stale),
        "latest_live_measure_utc": latest_iso,
        "latest_live_measure_age_hours": (
            round(age_hours, 1) if age_hours is not None else None
        ),
        "stale_threshold_hours": STALE_AFTER_HOURS,
        "fetch_status": fetch_status,
        "stations": stations_out,
    }


# --- Main -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mois",
        type=int,
        default=None,
        help="Forcer un mois (1-12) pour debug, sinon mois courant.",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("HydroPression Québec — Génération de l'état")
    print("HYDROPRESSURE_CLEAN_WFS_2026_07_10")
    print("=" * 60)

    mois_courant = determiner_mois_courant(args.mois)
    print(f"  Mois courant : {mois_courant} ({NOMS_MOIS[mois_courant]})")

    if args.mois:
        print("  ⚠️ Mois forcé via --mois (mode debug)")

    static = load_static_results()
    details = load_intervenants_detail()
    previous = load_previous_state()

    live, fetch_status = fetch_stations()

    if not fetch_status.get("ok"):
        print(f"  ⚠️ Source live indisponible : {fetch_status.get('error')}")
        print("     On continue avec le dernier état connu (mode dégradé).")

    state = compute_state(static, live, details, mois_courant, previous, fetch_status)
    state = _clean_for_json(state)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)

    size_kb = OUTPUT_PATH.stat().st_size / 1024

    print(f"\n✅ {OUTPUT_PATH.relative_to(ROOT.parent)} ({size_kb:.1f} KB)")
    print(f"   Mois utilisé : {NOMS_MOIS[mois_courant]}")
    print(f"   Généré à {state['generated_at']}")
    print(f"   data_stale = {state['data_stale']}")


if __name__ == "__main__":
    main()
