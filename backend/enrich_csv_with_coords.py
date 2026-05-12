#!/usr/bin/env python3
"""
enrich_csv_with_coords.py — v3 avec fallback sur les shapefiles CEHQ

Ajoute les colonnes lat et lon au CSV Phase 2 avec une cascade de 3 sources :

  Niveau 1 : API publique Vigilance (stations en service avec télémétrie)
             - Source primaire, ~270 stations
             - Coordonnées exactes des limnimètres en activité

  Niveau 2 : Shapefile officiel des stations FERMÉES (CEHQ / Données Québec)
             - Couvre les stations historiques désactivées
             - Coordonnées exactes (colonnes Latitude/Longitude en clair)
             - ~385 stations supplémentaires

  Niveau 3 : Shapefile officiel des stations OUVERTES (polygones de bassin)
             - Couvre les stations actuelles non diffusées par Vigilance
               (validation différée, partenaires non MELCCFP, etc.)
             - Coordonnée = CENTROÏDE du bassin versant (approximation)
             - À utiliser uniquement comme dernier recours

Gestion des codes composites (stations fusionnées) :
  Le CEHQ documente officiellement les codes composites comme "030345_41_34"
  qui signifient : station 030345 fusionnée avec 030341 et 030334.
  Pour ces cas, on essaie chaque composante du code, en commençant par la
  plus récente (premier code).

Sources :
- API Vigilance : geoegl.msp.gouv.qc.ca
- Shapefiles : donneesquebec.ca/recherche/dataset/stations-hydrometriques
"""

import requests
import pandas as pd
import geopandas as gpd
from pathlib import Path
from pyproj import Transformer

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "resultats_pression_phase2.csv"

# Chemins vers les shapefiles téléchargés depuis Données Québec
DOSSIER_SHP = ROOT / "data" / "stations_cehq"
SHP_FERMEES = DOSSIER_SHP / "BV_Stations_Fermees" / "Station_Fermee.shp"
SHP_OUVERTES = DOSSIER_SHP / "BV_ST_Ouvertes.shp"

API_URL = (
    "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi"
    "?service=wfs&version=1.1.0&request=getfeature"
    "&typename=stations_igo2_public&outputformat=geojson"
)


# ============================================================
# UTILITAIRES
# ============================================================

def variantes_code(code_composite):
    """
    Retourne toutes les variantes d'un code composite (stations fusionnées CEHQ).
    
    Exemples (documentation officielle CEHQ) :
      "011204_01"    → ["011204", "011201"]
      "030345_41_34" → ["030345", "030341", "030334"]
      "030299_91_62" → ["030299", "030291", "030262"]
    """
    parts = str(code_composite).split("_")
    base = parts[0].zfill(6)
    variantes = [base]
    if len(parts) > 1:
        prefix = base[:-len(parts[1])]
        for suffix in parts[1:]:
            variantes.append(prefix + suffix)
    return variantes


def code_normalise(c):
    """Normalise un code de station à 6 chiffres."""
    return str(c).strip().lstrip("0").zfill(6)


# ============================================================
# CHARGEMENT DES SOURCES
# ============================================================

def charger_source_api():
    """Niveau 1 : API Vigilance temps réel."""
    print("Niveau 1 — API Vigilance (CEHQ + partenaires)...")
    try:
        r = requests.get(API_URL, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ⚠️  Échec de l'API : {e}")
        return {}

    transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)
    coords = {}
    for f in data["features"]:
        code = str(f["properties"].get("station", "")).strip()
        if not code:
            continue
        c = f.get("geometry", {}).get("coordinates")
        if c and len(c) >= 2:
            lon, lat = transformer.transform(c[0], c[1])
            coords[code_normalise(code)] = (lat, lon)
    print(f"  {len(coords)} stations avec coordonnées exactes.")
    return coords


def charger_source_fermees():
    """Niveau 2 : Shapefile officiel des stations fermées."""
    print("\nNiveau 2 — Shapefile stations fermées (CEHQ / Données Québec)...")
    if not SHP_FERMEES.exists():
        print(f"  ⚠️  Fichier introuvable : {SHP_FERMEES}")
        print(f"     Télécharger depuis : https://donneesquebec.ca/recherche/dataset/stations-hydrometriques")
        return {}

    gdf = gpd.read_file(SHP_FERMEES)
    coords = {}
    for _, r in gdf.iterrows():
        # Le code "propre" est dans la colonne 'tp' (sans préfixe ST_)
        code = code_normalise(r['tp'])
        coords[code] = (r['Latitude'], r['Longitude'])
    print(f"  {len(coords)} stations fermées avec coordonnées exactes.")
    return coords


def charger_source_ouvertes():
    """Niveau 3 : Centroïdes des bassins versants des stations ouvertes."""
    print("\nNiveau 3 — Shapefile stations ouvertes (centroïdes BV, approximation)...")
    if not SHP_OUVERTES.exists():
        print(f"  ⚠️  Fichier introuvable : {SHP_OUVERTES}")
        return {}

    gdf = gpd.read_file(SHP_OUVERTES).to_crs("EPSG:4326")
    coords = {}
    for code, group in gdf.groupby("Station"):
        # Préférer le polygone de type "D" (Débit) si disponible
        sub = group[group["Type"] == "D"]
        if len(sub) == 0:
            sub = group
        centroid = sub.iloc[0].geometry.centroid
        coords[code_normalise(code)] = (centroid.y, centroid.x)
    print(f"  {len(coords)} bassins versants avec centroïde calculé.")
    return coords


# ============================================================
# CASCADE DE MATCHING
# ============================================================

def chercher_coords(code, coords_api, coords_fermees, coords_ouvertes):
    """
    Cascade en 3 niveaux avec gestion des codes composites.
    Retourne (lat, lon, source) où source ∈ {api, fermee, ouverte_BV, ABSENT}.
    """
    for variante in variantes_code(code):
        if variante in coords_api:
            lat, lon = coords_api[variante]
            return lat, lon, "api"
        if variante in coords_fermees:
            lat, lon = coords_fermees[variante]
            return lat, lon, "fermee"
        if variante in coords_ouvertes:
            lat, lon = coords_ouvertes[variante]
            return lat, lon, "ouverte_BV"
    return None, None, "ABSENT"


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Enrichissement des coordonnées (3 sources en cascade)")
    print("=" * 60)
    print()

    coords_api = charger_source_api()
    coords_fermees = charger_source_fermees()
    coords_ouvertes = charger_source_ouvertes()

    if not CSV_PATH.exists():
        print(f"\n❌ CSV introuvable : {CSV_PATH}")
        return

    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    print(f"\nEnrichissement de {len(df)} stations...")

    lats, lons, sources = [], [], []
    for code in df["station"]:
        lat, lon, source = chercher_coords(
            code, coords_api, coords_fermees, coords_ouvertes
        )
        lats.append(lat)
        lons.append(lon)
        sources.append(source)

    df["lat"] = lats
    df["lon"] = lons
    df["source_coords"] = sources

    n_ok = df["lat"].notna().sum()
    print(f"\n=== RÉSULTAT ===")
    print(f"  Stations avec coordonnées : {n_ok}/{len(df)}")
    print(f"\n  Répartition des sources :")
    for src, n in df["source_coords"].value_counts().items():
        label = {
            "api": "API Vigilance (exact, temps réel)",
            "fermee": "Shapefile stations fermées (exact, archive CEHQ)",
            "ouverte_BV": "Centroïde BV stations ouvertes (approximatif)",
            "ABSENT": "Aucune source trouvée",
        }.get(src, src)
        print(f"    {src:<12} {n:>4}   ({label})")

    if (df["source_coords"] == "ABSENT").sum() > 0:
        print(f"\n  ⚠️  Stations sans coordonnées :")
        sans = df[df["source_coords"] == "ABSENT"][["station", "nom"]]
        print(sans.to_string(index=False))

    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print(f"\n✅ {CSV_PATH.name} mis à jour avec lat/lon/source_coords")


if __name__ == "__main__":
    main()
