#!/usr/bin/env python3
"""
enrich_csv_with_coords.py — utilitaire à lancer une seule fois

Ajoute les colonnes lat et lon au CSV Phase 2 en récupérant les coordonnées
depuis l'API CEHQ. Sortie : data/resultats_pression_phase2.csv enrichi.
"""
import requests
import pandas as pd
from pathlib import Path
from pyproj import Transformer

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "resultats_pression_phase2.csv"

API_URL = (
    "https://geoegl.msp.gouv.qc.ca/apis/mapserver-vigilance/ws/vigilance.fcgi"
    "?service=wfs&version=1.1.0&request=getfeature"
    "&typename=stations_igo2_public&outputformat=geojson"
)

print("Téléchargement des coordonnées depuis l'API CEHQ...")
r = requests.get(API_URL, timeout=60)
r.raise_for_status()
data = r.json()

transformer = Transformer.from_crs("EPSG:32198", "EPSG:4326", always_xy=True)

coords = {}
for f in data["features"]:
    code = str(f["properties"].get("station", "")).strip()
    if not code:
        continue
    geom = f.get("geometry", {})
    c = geom.get("coordinates")
    if c and len(c) >= 2:
        lon, lat = transformer.transform(c[0], c[1])
        coords[code] = (lat, lon)

print(f"  {len(coords)} stations avec coordonnées récupérées")

df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
df["lat"] = df["station"].astype(str).map(lambda c: coords.get(c, (None, None))[0])
df["lon"] = df["station"].astype(str).map(lambda c: coords.get(c, (None, None))[1])

n_with_coords = df["lat"].notna().sum()
print(f"  {n_with_coords}/{len(df)} stations enrichies")

df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
print(f"✅ {CSV_PATH.name} mis à jour avec lat/lon")
