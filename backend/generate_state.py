#!/usr/bin/env python3
# HYDROPRESSION_CEHQ_SUIVIHYDRO_2026_07_10
"""
generate_state.py — HydroPression Québec (v3.0)

Génère web/data/etat_pression.json consommé par la web app.

--- CHANGEMENT DE SOURCE 2026-07 ----------------------------------------------
L'agrégateur MSP/Vigilance (geoegl.msp.gouv.qc.ca) est doublement hors service :
  1. un challenge anti-bot bloque désormais tout client automatisé (GitHub
     Actions, scripts, et même les requêtes XHR de navigateurs réels) ;
  2. son flux stations_igo2_public est gelé au 5-6 juin 2026 (l'ancienne app
     Vigilance est en cours de fermeture).

La source de débits en quasi temps réel est donc désormais le SUIVI
HYDROLOGIQUE DU CEHQ (MELCCFP) — la source primaire, vivante et souveraine :

    https://www.cehq.gouv.qc.ca/suivihydro/fichier_donnees.asp?NoStation=XXXXXX

Chaque station renvoie un fichier texte tabulé (Date / Heure / Débit),
valeurs les plus récentes en premier, décimales à virgule, pas de 15 min.
Les codes composites (ex. "010902_03", stations fusionnées) sont résolus via
variantes_code() : on interroge chaque variante jusqu'à obtenir un débit.

Le calcul (pression mensuelle, étiage, catégories) est inchangé.
Les métadonnées data_stale / fetch_status / latest_live_measure_utc restent
exposées pour le bandeau de fraîcheur du front.

Usage :
    python generate_state.py [--mois N]

Variables d'environnement (optionnelles) :
    HP_STALE_AFTER_HOURS        seuil de péremption en heures      (défaut 6)
    HP_FETCH_RETRIES            tentatives réseau par station      (défaut 2)
    HP_FETCH_TIMEOUT            timeout par requête en secondes    (défaut 15)
    HP_CEHQ_UTC_OFFSET_HOURS    décalage horaire des mesures CEHQ  (défaut -5,
                                heure normale de l'Est, convention des données
                                hydrométriques du Québec)
    HP_REQUEST_DELAY_S          pause entre stations en secondes   (défaut 0.12)
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- Configuration ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "resultats_pression_phase2.csv"
DETAIL_PATH = ROOT / "data" / "details_intervenants.csv"
OUTPUT_PATH = ROOT.parent / "web" / "data" / "etat_pression.json"

CEHQ_BASE = "https://www.cehq.gouv.qc.ca/suivihydro"
CEHQ_FICHIER_URL = CEHQ_BASE + "/fichier_donnees.asp?NoStation={code}"
CEHQ_GRAPHIQUE_URL = CEHQ_BASE + "/graphique.asp?NoStation={code}"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/plain, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Referer": CEHQ_BASE + "/default.asp",
    "Connection": "keep-alive",
}

STALE_AFTER_HOURS = float(os.environ.get("HP_STALE_AFTER_HOURS", "6"))
FETCH_RETRIES = int(os.environ.get("HP_FETCH_RETRIES", "2"))
FETCH_TIMEOUT = int(os.environ.get("HP_FETCH_TIMEOUT", "15"))
CEHQ_UTC_OFFSET_HOURS = float(os.environ.get("HP_CEHQ_UTC_OFFSET_HOURS", "-5"))
REQUEST_DELAY_S = float(os.environ.get("HP_REQUEST_DELAY_S", "0.12"))

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


def variantes_code(code_composite) -> list[str]:
    """
    Variantes d'un code composite (stations fusionnées CEHQ).
      "011204_01"    -> ["011204", "011201"]
      "030345_41_34" -> ["030345", "030341", "030334"]
    Un code simple retourne [lui-même].
    """
    parts = str(code_composite).strip().split("_")
    base = parts[0].zfill(6)
    variantes = [base]
    if len(parts) > 1:
        for suffix in parts[1:]:
            variantes.append(base[: -len(suffix)] + suffix)
    return variantes


def _parse_fichier_cehq(text: str):
    """
    Parse le fichier texte suivihydro (colonnes tabulées, plus récent en 1er).
    Retourne (debit, niveau, date_locale 'YYYY-MM-DD HH:MM') de la mesure la
    plus récente ayant une valeur, ou (None, None, None).
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None, None, None

    header = [h.strip().lower() for h in lines[0].split("\t")]
    idx_debit = idx_niveau = None
    for i, h in enumerate(header):
        if "bit" in h:      # "débit" robuste aux soucis d'encodage
            idx_debit = i
        elif "iveau" in h:  # "niveau"
            idx_niveau = i

    def to_num(cell):
        cell = cell.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
        if not cell:
            return None
        try:
            return float(cell)
        except ValueError:
            return None

    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) < 3 or not re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0].strip()):
            continue
        debit = to_num(cells[idx_debit]) if idx_debit is not None and idx_debit < len(cells) else None
        niveau = to_num(cells[idx_niveau]) if idx_niveau is not None and idx_niveau < len(cells) else None
        if debit is not None or niveau is not None:
            return debit, niveau, cells[0].strip() + " " + cells[1].strip()
    return None, None, None


def _fetch_station_cehq(session, code: str):
    """
    Interroge le fichier temps réel d'une station. Retourne (record|None, statut).
    statut : "ok", "inexistante", "vide", "challenge" ou "erreur".
    """
    url = CEHQ_FICHIER_URL.format(code=code)
    last_status = "erreur"
    for tentative in range(1, FETCH_RETRIES + 1):
        try:
            r = session.get(url, timeout=FETCH_TIMEOUT, headers=REQUEST_HEADERS)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            body = r.text

            if _looks_like_html(body):
                raise ChallengeDetected("réponse HTML/challenge au lieu du fichier texte")
            if "inexistante" in body.lower():
                return None, "inexistante"

            debit, niveau, date_locale = _parse_fichier_cehq(body)
            if debit is None and niveau is None:
                return None, "vide"

            # Horodatage local CEHQ -> UTC (convention : heure normale de l'Est)
            tz = timezone(timedelta(hours=CEHQ_UTC_OFFSET_HOURS))
            try:
                dt_local = datetime.strptime(date_locale, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                date_iso = dt_local.isoformat()
            except ValueError:
                dt_local, date_iso = None, None

            return {
                "debit_obs_m3s": debit,
                "niveau_m": niveau,
                "date_mesure": date_iso,
                "dt_utc": dt_local.astimezone(timezone.utc) if dt_local else None,
                "etat": None,
                "url_cehq": CEHQ_GRAPHIQUE_URL.format(code=code),
                "lon": None,
                "lat": None,
            }, "ok"

        except ChallengeDetected:
            last_status = "challenge"
        except (requests.RequestException, ValueError):
            last_status = "erreur"
        if tentative < FETCH_RETRIES:
            time.sleep(1.5 * tentative)
    return None, last_status


def fetch_stations() -> tuple[dict, dict]:
    """
    Interroge le suivi hydrologique CEHQ station par station (codes du CSV,
    variantes composites incluses). Retourne (stations, meta).
    """
    meta = {
        "ok": False,
        "error": None,
        "n_features": 0,
        "n_avec_debit": 0,
        "latest_measure_utc": None,
        "source": "cehq.gouv.qc.ca/suivihydro (fichier_donnees.asp)",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "n_stations_interrogees": 0,
        "n_introuvables": 0,
        "n_erreurs": 0,
        "n_challenges": 0,
    }

    try:
        static = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    except OSError as e:
        meta["error"] = "CSV introuvable : " + str(e)
        return {}, meta

    codes_bruts = [str(c).strip() for c in static["station"].tolist()]
    session = requests.Session()

    out = {}
    latest = None
    tous_dts = []
    t0 = time.time()
    print(f"[{datetime.now():%H:%M:%S}] Interrogation du suivihydro CEHQ "
          f"({len(codes_bruts)} stations)...")

    for i, code_brut in enumerate(codes_bruts, 1):
        meta["n_stations_interrogees"] += 1
        record, statut = None, "inexistante"
        for variante in variantes_code(code_brut):
            record, statut = _fetch_station_cehq(session, variante)
            if record is not None:
                record["code_temps_reel"] = variante
                break
            if statut in ("challenge", "erreur"):
                break  # réseau cassé : inutile d'insister sur les variantes

        key = normalize_code(code_brut)
        if record is not None:
            out[key] = record
            if record["debit_obs_m3s"] is not None:
                meta["n_avec_debit"] += 1
            dt = record.pop("dt_utc", None)
            if dt is not None:
                tous_dts.append(dt)
                if latest is None or dt > latest:
                    latest = dt
        elif statut in ("inexistante", "vide"):
            meta["n_introuvables"] += 1
        elif statut == "challenge":
            meta["n_challenges"] += 1
        else:
            meta["n_erreurs"] += 1

        if i % 25 == 0:
            print(f"  ... {i}/{len(codes_bruts)} stations "
                  f"({meta['n_avec_debit']} débits, {time.time()-t0:.0f}s)")
        time.sleep(REQUEST_DELAY_S)

    meta["n_features"] = len(out)
    meta["latest_measure_utc"] = latest.isoformat() if latest else None
    if tous_dts:
        tous_dts.sort()
        meta["median_measure_utc"] = tous_dts[len(tous_dts) // 2].isoformat()
    else:
        meta["median_measure_utc"] = None

    if meta["n_challenges"] > 0 and meta["n_avec_debit"] == 0:
        meta["error"] = ("le serveur CEHQ renvoie une page HTML/challenge — "
                         "client probablement bloqué (IP ou en-têtes)")
    elif meta["n_avec_debit"] == 0:
        meta["error"] = ("aucun débit exploitable ("
                         + str(meta["n_erreurs"]) + " erreurs réseau, "
                         + str(meta["n_introuvables"]) + " stations sans fichier)")
    else:
        meta["ok"] = True

    print(f"  {meta['n_avec_debit']} stations avec débit, "
          f"{meta['n_introuvables']} sans fichier temps réel, "
          f"{meta['n_erreurs']} erreurs, {meta['n_challenges']} challenges "
          f"en {time.time()-t0:.0f}s")
    if latest:
        age_h = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
        print(f"  Mesure la plus récente : {latest.isoformat()} (il y a {max(age_h,0):.1f} h)")
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
        age_hours = max(0.0, (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600.0)

    # La péremption est jugée sur la MÉDIANE des horodatages : une poignée de
    # stations encore vivantes ne doit pas masquer un gel généralisé.
    median_iso = fetch_status.get("median_measure_utc") or latest_iso
    median_dt = _parse_utc(median_iso)
    age_median_hours = None
    if median_dt is not None:
        age_median_hours = max(0.0, (datetime.now(timezone.utc) - median_dt).total_seconds() / 3600.0)

    # data_stale si : le fetch a échoué, OU aucune station live,
    # OU la mesure la plus récente dépasse le seuil de péremption.
    age_ref = age_median_hours if age_median_hours is not None else age_hours
    data_stale = (
        not fetch_status.get("ok", False)
        or n_updated == 0
        or (age_ref is not None and age_ref > STALE_AFTER_HOURS)
    )
    if data_stale:
        raison = (
            fetch_status.get("error")
            or ("aucune station avec débit live" if n_updated == 0 else None)
            or (f"source périmée : âge médian des mesures {age_ref:.1f} h "
                f"(seuil {STALE_AFTER_HOURS} h)" if age_ref is not None else "cause inconnue")
        )
        print(f"  🔴 DONNÉES NON À JOUR — {raison}")
    else:
        age_txt = f"{age_hours:.1f} h" if age_hours is not None else "inconnu"
        print(f"  🟢 Données à jour (dernière mesure il y a {age_txt})")

    return {
        "version": "3.0-cehq-suivihydro",
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
        "median_live_measure_age_hours": round(age_median_hours, 1) if age_median_hours is not None else None,
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
    print("HYDROPRESSION_CEHQ_SUIVIHYDRO_2026_07_10")
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
