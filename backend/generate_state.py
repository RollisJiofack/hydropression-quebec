#!/usr/bin/env python3
"""
generate_state.py — HydroPression Québec

Génère etat_pression.json consommé par la web app.

Sources :
1. CSV statique :
   - resultats_pression_phase2.csv (12 colonnes mensuelles + étiage)
   - details_intervenants.csv (12 valeurs mensuelles par préleveur)
2. API publique MSP / Vigilance (débits actuels agrégés du CEHQ, EC, etc.).

Logique de calcul de la pression actuelle :
- On détermine le MOIS COURANT (selon la date du jour, fuseau Toronto).
- Pour chaque station, on prend le débit consommé du mois courant.
- On calcule en live :
    pression_actuelle = consommation_mois / (débit_observé_live + consommation_mois) × 100

Logique pour la pression d'étiage :
- Toujours basée sur le débit consommé MAX(juillet, août) calculé statiquement.
- Comparaison au Q2,7 estival.

--- CORRECTIFS 2026-07 -------------------------------------------------------
Le service geoegl.msp.gouv.qc.ca a placé un challenge anti-bot devant l'API.
Un User-Agent non-navigateur (ancien "HydroPression-Quebec/2.0") reçoit une
page HTML "Please enable JavaScript" au lieu du GeoJSON ; r.json() levait alors
une exception avalée silencieusement par le try/except, ce qui figeait le site
sur les dernières valeurs connues (n_stations_debit_live = 0) sans le signaler.

Ce module :
  * envoie des en-têtes de navigateur réalistes ;
  * réessaie avec back-off ;
  * DÉTECTE une réponse de type challenge/HTML et échoue avec un message clair ;
  * expose fetch_status + data_stale + latest_live_measure_utc dans le JSON de
    sortie, pour que le front puisse afficher un bandeau "données non à jour"
    plutôt que de servir du gelé en silence ;
  * détecte la PÉREMPTION de la source : si même la mesure live la plus récente
    dépasse HP_STALE_AFTER_HOURS heures, data_stale=True.

Usage :
    python generate_state.py [--mois N]   (--mois pour forcer un mois précis, debug)

Variables d'environnement (optionnelles) :
    HP_STALE_AFTER_HOURS   seuil de péremption en heures        (défaut 6)
    HP_FETCH_RETRIES       nombre de tentatives réseau           (défaut 3)
    HP_FETCH_TIMEOUT       timeout par tentative en secondes     (défaut 60)
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
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

STATIONS_PAGE_URL = (
    "https://vigilance.geo.msp.gouv.qc.ca/stations"
    "?sort=a_etat_max.desc.nullslast,e_plan_deau"
    "&numberPerPage=10"
    "&a_etat_max=&b_label=&c_mun=&d_regadmin=&e_plan_deau="
    "&f_mrc=&g_bassin_versant=&trend_pct=&prev=false&page=1"
)

USE_BROWSER_FALLBACK = os.environ.get("HP_USE_BROWSER_FALLBACK", "1") != "0"
BROWSER_TIMEOUT_MS = int(os.environ.get("HP_BROWSER_TIMEOUT_MS", "90000"))
BROWSER_WAIT_MS = int(os.environ.get("HP_BROWSER_WAIT_MS", "8000"))

# En-têtes de navigateur : un User-Agent applicatif custom déclenche le
# challenge anti-bot du serveur MSP. On se présente comme un navigateur réel.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, application/geo+json, text/plain, */*",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Referer": "https://vigilance.geo.msp.gouv.qc.ca/",
    "Connection": "keep-alive",
}

STALE_AFTER_HOURS = float(os.environ.get("HP_STALE_AFTER_HOURS", "6"))
FETCH_RETRIES = int(os.environ.get("HP_FETCH_RETRIES", "3"))
FETCH_TIMEOUT = int(os.environ.get("HP_FETCH_TIMEOUT", "60"))

NOMS_MOIS = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "août",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}


class ChallengeDetected(RuntimeError):
    """La réponse ressemble à une page anti-bot / HTML, pas à du GeoJSON."""


# --- Utilitaires ----------------------------------------------------------

def normalize_code(code) -> str:
    if code is None or pd.isna(code):
        return ""
    return str(code).strip().lstrip("0")


def _looks_like_html(text: str) -> bool:
    """Détecte une page de challenge / HTML renvoyée à la place du GeoJSON."""
    head = text.lstrip()[:600].lower()
    signaux = (
        "<!doctype html",
        "<html",
        "enable javascript",
        "please enable",
        "captcha",
        "incapsula",
        "cf-browser-verification",
        "challenge-platform",
    )
    return any(s in head for s in signaux)


def _parse_utc(value):
    """Parse un horodatage ISO en datetime UTC (gère offsets, 'Z', fractions)."""
    if not value:
        return None
    txt = str(value).strip()
    # 1) fromisoformat gère 'T', les offsets (+00:00) et 'Z' (Python 3.11+).
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # 2) Repli : formats sans offset.
    base = txt.replace("Z", "").split("+")[0].split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(base, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def fetch_geojson_with_browser():
    """
    Repli navigateur pour contourner le challenge JavaScript de Vigilance/MSP.

    Le navigateur charge d'abord la page officielle des stations, ce qui permet
    au site d'établir sa session/cookies. Ensuite, l'appel GeoJSON est relancé
    depuis le même contexte navigateur.
    """
    if not USE_BROWSER_FALLBACK:
        return None, "fallback navigateur désactivé par HP_USE_BROWSER_FALLBACK=0"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return None, f"Playwright non installé: {e}"

    print("  🌐 Repli navigateur Playwright vers la page Vigilance/MSP...")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                locale="fr-CA",
                timezone_id="America/Toronto",
                viewport={"width": 1366, "height": 900},
                extra_http_headers={
                    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
                },
            )

            page = context.new_page()

            page.goto(
                STATIONS_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT_MS,
            )

            # Laisser le JS/challenge/session se stabiliser.
            page.wait_for_timeout(BROWSER_WAIT_MS)

            # Relancer l'appel GeoJSON depuis le navigateur, pas depuis requests.
            result = page.evaluate(
                """
                async (url) => {
                  const r = await fetch(url, {
                    method: "GET",
                    credentials: "include",
                    headers: {
                      "Accept": "application/json, application/geo+json, text/plain, */*"
                    }
                  });
                  return {
                    ok: r.ok,
                    status: r.status,
                    contentType: r.headers.get("content-type") || "",
                    text: await r.text()
                  };
                }
                """,
                API_URL,
            )

            browser.close()

        ctype = (result.get("contentType") or "").lower()
        body = result.get("text") or ""

        if not result.get("ok"):
            return None, f"browser fetch HTTP {result.get('status')}"

        if "html" in ctype or _looks_like_html(body):
            return None, f"browser fetch encore bloqué par HTML/challenge (Content-Type={ctype})"

        return json.loads(body), None

    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def fetch_stations() -> tuple[dict, dict]:
    """
    Interroge l'API MSP/Vigilance et retourne (stations, meta).

    meta = {
        "ok": bool, "error": str|None, "n_features": int,
        "n_avec_debit": int, "latest_measure_utc": str|None,
        "source": str, "fetched_at": str,
    }
    """
    meta = {
        "ok": False,
        "error": None,
        "n_features": 0,
        "n_avec_debit": 0,
        "latest_measure_utc": None,
        "source": "geoegl.msp.gouv.qc.ca/mapserver-vigilance",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    last_exc = None
    data = None
    for tentative in range(1, FETCH_RETRIES + 1):
        print(f"[{datetime.now():%H:%M:%S}] Appel API MSP/Vigilance "
              f"(tentative {tentative}/{FETCH_RETRIES})...")
        t0 = time.time()
        try:
            r = requests.get(API_URL, timeout=FETCH_TIMEOUT, headers=BROWSER_HEADERS)
            r.raise_for_status()

            ctype = r.headers.get("Content-Type", "").lower()
            body = r.text

            # Le serveur peut renvoyer 200 + HTML (page de challenge anti-bot).
            if "html" in ctype or _looks_like_html(body):
                raise ChallengeDetected(
                    f"réponse HTML/challenge (Content-Type={ctype or 'inconnu'}). "
                    "Le serveur bloque probablement le client (User-Agent ou IP)."
                )

            data = r.json()
            print(f"  Réponse reçue en {time.time()-t0:.1f}s")
            break

        except ChallengeDetected as e:
            last_exc = e
            print(f"  ⚠️  {e}")
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            print(f"  ⚠️  Échec réseau/parse : {e}")

        if tentative < FETCH_RETRIES:
            attente = 2 ** tentative
            print(f"     Nouvelle tentative dans {attente}s...")
            time.sleep(attente)

    if data is None:
    meta["error"] = f"{type(last_exc).__name__}: {last_exc}"

    # Repli navigateur : nécessaire depuis que Vigilance/MSP renvoie un challenge
    # JavaScript aux clients automatisés simples.
    browser_data, browser_error = fetch_geojson_with_browser()

    if browser_data is None:
        meta["error"] = f"{meta['error']} | BrowserFallback: {browser_error}"
        return {}, meta

    data = browser_data
    meta["error"] = None
    print("  ✅ Données GeoJSON récupérées via le repli navigateur Playwright")

    features = data.get("features", [])
    meta["n_features"] = len(features)
    if not features:
        meta["error"] = "API sans station exploitable (0 feature)"
        return {}, meta

    transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)

    out = {}
    latest = None
    n_avec_debit = 0
    for f in features:
        p = f["properties"]
        code = normalize_code(p.get("station"))
        if not code:
            continue
        coords = f.get("geometry", {}).get("coordinates")
        lon = lat = None
        if coords and len(coords) >= 2:
            lon, lat = transformer.transform(coords[0], coords[1])

        debit = p.get("dern_valeur_deb")
        if debit is not None:
            n_avec_debit += 1

        date_mesure = p.get("dern_date_prise_valeur_utc")
        dt = _parse_utc(date_mesure)
        if dt is not None and (latest is None or dt > latest):
            latest = dt

        out[code] = {
            "debit_obs_m3s": debit,
            "niveau_m": p.get("dern_valeur_niv"),
            "date_mesure": date_mesure,
            "etat": p.get("etat"),
            "url_cehq": p.get("fournisseur_url"),
            "lon": lon,
            "lat": lat,
        }

    meta["ok"] = True
    meta["n_avec_debit"] = n_avec_debit
    meta["latest_measure_utc"] = latest.isoformat() if latest else None

    print(f"  {len(out)} stations reçues, {n_avec_debit} avec un débit publié")
    if latest:
        age_h = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
        print(f"  Mesure la plus récente : {latest.isoformat()} "
              f"(il y a {age_h:.1f} h)")
    return out, meta


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
    Charge le dernier JSON publié pour éviter qu'une panne API remplace
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
    fetch_status: dict | None = None,
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

        # Débit observé : live, CSV, puis dernier état connu si l'API est muette.
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

    # --- Statut de fraîcheur ------------------------------------------------
    fetch_status = fetch_status or {}
    latest_iso = fetch_status.get("latest_measure_utc")
    latest_dt = _parse_utc(latest_iso)
    age_hours = None
    if latest_dt is not None:
        age_hours = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600.0

    # data_stale si : le fetch a échoué, OU aucune station live,
    # OU la mesure la plus récente dépasse le seuil de péremption.
    data_stale = (
        not fetch_status.get("ok", False)
        or n_updated == 0
        or (age_hours is not None and age_hours > STALE_AFTER_HOURS)
    )
    if data_stale:
        raison = (
            fetch_status.get("error")
            or ("aucune station avec débit live" if n_updated == 0 else None)
            or (f"source périmée : dernière mesure il y a {age_hours:.1f} h "
                f"(seuil {STALE_AFTER_HOURS} h)" if age_hours is not None else "cause inconnue")
        )
        print(f"  🔴 DONNÉES NON À JOUR — {raison}")
    else:
        age_txt = f"{age_hours:.1f} h" if age_hours is not None else "inconnu"
        print(f"  🟢 Données à jour (dernière mesure il y a {age_txt})")

    return {
        "version": "2.1",
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
        # --- Nouveau : visibilité sur la fraîcheur de la donnée live ---------
        "data_stale": bool(data_stale),
        "latest_live_measure_utc": latest_iso,
        "latest_live_measure_age_hours": round(age_hours, 1) if age_hours is not None else None,
        "stale_threshold_hours": STALE_AFTER_HOURS,
        "fetch_status": fetch_status,
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

    live, fetch_status = fetch_stations()
    if not fetch_status.get("ok"):
        print(f"  ⚠️  API indisponible : {fetch_status.get('error')}")
        print(f"     On continue avec le dernier état connu (mode dégradé).")

    state = compute_state(static, live, details, mois_courant, previous, fetch_status)
    state = _clean_for_json(state)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\n✅ {OUTPUT_PATH.relative_to(ROOT.parent)} ({size_kb:.1f} KB)")
    print(f"   Mois utilisé : {NOMS_MOIS[mois_courant]}")
    print(f"   Généré à {state['generated_at']}")
    print(f"   data_stale = {state['data_stale']}")


if __name__ == "__main__":
    main()
