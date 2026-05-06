#!/usr/bin/env python3
"""
build_details.py — HydroPression Québec

Reconstruit deux fichiers à partir du fichier Excel des déclarations :

1. resultats_pression_phase2.csv  (recalcul de la pression par station)
2. details_intervenants.csv       (détail des préleveurs amont par station)

Méthode (option β validée) :
- Volume = moyenne mensuelle sur les 5 dernières années (2020-2024) par site
- Filtre Surface uniquement
- Période estivale juin-octobre (cohérence avec Q2,7 estival)
- Sites exclus s'ils n'ont aucune déclaration sur 2020-2024 (sites zombies)

Ce script remplace l'ancien notebook Phase 2. Il est plus rigoureux
méthodologiquement car il capture le comportement structurel des
préleveurs sur 5 ans plutôt qu'une photo annuelle.

Usage :
    cd backend
    python build_details.py
"""

import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point


# ============================================================
# CONFIGURATION — à adapter selon ton installation
# ============================================================

# Adapte ces deux chemins à ton installation
CHEMIN_EXCEL = Path(r"C:\Users\jioro01\Desktop\pression-eau-quebec\prelevements-eau-declares-depuis-2012.xlsx")
CHEMIN_RESEAU = Path(r"C:\Users\jioro01\Desktop\pression-eau-quebec\Atlas de l'eau\AtlasH2020_EA_HP.shp")

# Le reste est relatif au script
ROOT = Path(__file__).resolve().parent
CHEMIN_ETIAGES = ROOT / "data" / "debits_etiage_cehq.csv"

OUT_PRESSION = ROOT / "data" / "resultats_pression_phase2.csv"
OUT_DETAILS = ROOT / "data" / "details_intervenants.csv"

# Paramètres méthodologiques
ANNEE_MIN_PROFIL = 2020         # Moyenne sur 2020-2024 (5 ans)
ANNEE_MAX_PROFIL = 2024
MOIS_ESTIVAL = (6, 10)          # Juin à octobre (5 mois)
DIST_MAX_SNAP_M = 2000          # Snap d'un site sur le réseau

# Top affiché par station dans le détail
TOP_N_INTERVENANTS = 20


# ============================================================
# UTILITAIRES
# ============================================================

def normalize_code(code) -> str:
    """Code de station normalisé (sans zéro initial)."""
    if code is None or pd.isna(code):
        return ""
    return str(code).strip().lstrip("0")


def safe_num(v):
    if v is None or pd.isna(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================
# ÉTAPE 1 — Charger et filtrer les déclarations
# ============================================================

def charger_prelevements_5ans():
    """
    Charge les onglets 2020-2024, filtre Surface + estival,
    calcule un débit journalier par ligne (mois × site).
    """
    print("=" * 68)
    print("ÉTAPE 1 — Chargement des déclarations de prélèvements (2020-2024)")
    print("=" * 68)
    
    if not CHEMIN_EXCEL.exists():
        print(f"❌ Fichier Excel introuvable : {CHEMIN_EXCEL}")
        print(f"   → Modifie CHEMIN_EXCEL en haut de ce script")
        sys.exit(1)
    
    annees = list(range(ANNEE_MIN_PROFIL, ANNEE_MAX_PROFIL + 1))
    dfs = []
    for an in annees:
        try:
            t0 = time.time()
            df = pd.read_excel(CHEMIN_EXCEL, sheet_name=str(an))
            df["_annee_source"] = an
            print(f"  {an} : {len(df):>7,} lignes lues en {time.time()-t0:.1f}s")
            dfs.append(df)
        except Exception as e:
            print(f"  {an} : ⚠️  non chargé ({e})")
    
    df = pd.concat(dfs, ignore_index=True)
    df = df.replace("NULL", np.nan)
    
    # Conversions numériques
    cols_num = [
        "Longitude (site)", "Latitude (site)",
        "Volume ventilé par code SCIAN par site (L)",
        "Volume total mensuel par site (L)",
        "Nombre de jours/mois", "Année", "Mois",
    ]
    for c in cols_num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    
    print(f"\n  Total lignes brutes : {len(df):,}")
    
    # Filtre 1 : Surface uniquement (exclut Souterrain et Aqueduc/NULL)
    df = df[df["Source du prélèvement"] == "Surface"].copy()
    print(f"  Après filtre Surface : {len(df):,}")
    
    # Filtre 2 : coordonnées valides
    df = df.dropna(subset=["Longitude (site)", "Latitude (site)"])
    print(f"  Avec coordonnées : {len(df):,}")
    
    # Filtre 3 : période estivale juin-octobre
    df = df[df["Mois"].between(MOIS_ESTIVAL[0], MOIS_ESTIVAL[1])]
    print(f"  Période estivale (juin-oct) : {len(df):,}")
    
    # Calcul du débit journalier par ligne
    # vol_mensuel (L) / nb_jours_actifs / 86400 / 1000 = m³/s
    df["debit_jour_m3s"] = (
        df["Volume ventilé par code SCIAN par site (L)"]
        / df["Nombre de jours/mois"].replace(0, np.nan)
        / 86400 / 1000
    )
    df = df[df["debit_jour_m3s"].notna() & (df["debit_jour_m3s"] > 0)]
    print(f"  Débit valide : {len(df):,}")
    
    return df


# ============================================================
# ÉTAPE 2 — Profil 5 ans par site
# ============================================================

def calculer_profil_sites(df_decl):
    """
    Pour chaque site : moyenne du débit journalier sur les années
    où il a déclaré, mois par mois.
    """
    print("\n" + "=" * 68)
    print("ÉTAPE 2 — Profil 5 ans par site")
    print("=" * 68)
    
    # Étape 2a : moyenne par (site × année × mois) → un seul débit par mois/an
    profil_annee = df_decl.groupby(
        ["Numéro du prélèvement", "Année", "Mois"]
    )["debit_jour_m3s"].mean().reset_index()
    
    # Étape 2b : moyenne sur les années pour chaque (site × mois)
    profil_mois = profil_annee.groupby(
        ["Numéro du prélèvement", "Mois"]
    )["debit_jour_m3s"].mean().reset_index()
    
    # Étape 2c : pivoter pour avoir une colonne par mois
    pivot = profil_mois.pivot(
        index="Numéro du prélèvement",
        columns="Mois",
        values="debit_jour_m3s"
    ).fillna(0)
    pivot.columns = [f"debit_mois_{int(m):02d}_m3s" for m in pivot.columns]
    
    # Débit estival moyen (juin-oct)
    pivot["debit_estival_m3s"] = pivot.mean(axis=1)
    
    # Métadonnées par site
    meta_cols = {
        "longitude": "Longitude (site)",
        "latitude": "Latitude (site)",
        "intervenant": [c for c in df_decl.columns if "intervenant" in c.lower() and "Nom" in c][0],
        "municipalite": "Municipalité",
        "secteur_scian": "Description du code SCIAN",
        "code_scian": "Code SCIAN par site par mois",
    }
    meta = df_decl.groupby("Numéro du prélèvement").agg({
        meta_cols["longitude"]: "first",
        meta_cols["latitude"]: "first",
        meta_cols["intervenant"]: "first",
        meta_cols["municipalite"]: "first",
        meta_cols["secteur_scian"]: "first",
        meta_cols["code_scian"]: "first",
        "Année": ["min", "max"],
    })
    # Aplatir les colonnes multi-niveau
    meta.columns = [
        "longitude", "latitude", "intervenant",
        "municipalite", "secteur_scian", "code_scian",
        "premiere_annee", "derniere_annee",
    ]
    
    # Volume annuel moyen sur la période active
    df_vol_an = df_decl.groupby(["Numéro du prélèvement", "Année"])[
        "Volume ventilé par code SCIAN par site (L)"
    ].sum().reset_index()
    vol_moyen = df_vol_an.groupby("Numéro du prélèvement")[
        "Volume ventilé par code SCIAN par site (L)"
    ].mean()
    meta["volume_annuel_moyen_Mm3"] = vol_moyen / 1e9
    
    sites = meta.join(pivot)
    sites = sites.reset_index()
    
    # Filtre 4 : exclure les sites sans déclaration sur 2020-2024
    # (déjà appliqué de fait puisqu'on n'a chargé que ces années,
    # mais on confirme ici pour clarté)
    sites = sites[sites["debit_estival_m3s"] > 0].copy()
    
    print(f"  Sites avec profil 5 ans : {len(sites):,}")
    print(f"  Débit estival total cumulé : {sites['debit_estival_m3s'].sum():.2f} m³/s")
    
    return sites


# ============================================================
# ÉTAPE 3 — Réseau hydrographique et stations
# ============================================================

def charger_reseau_et_stations():
    """Réseau hydro + Q2,7 + (les coordonnées des stations seront enrichies après)."""
    print("\n" + "=" * 68)
    print("ÉTAPE 3 — Réseau hydrographique et débits d'étiage")
    print("=" * 68)
    
    if not CHEMIN_RESEAU.exists():
        print(f"❌ Réseau hydrographique introuvable : {CHEMIN_RESEAU}")
        print(f"   → Modifie CHEMIN_RESEAU en haut de ce script")
        sys.exit(1)
    
    if not CHEMIN_ETIAGES.exists():
        print(f"❌ CSV étiages introuvable : {CHEMIN_ETIAGES}")
        print(f"   → Le fichier debits_etiage_cehq.csv doit être dans data/")
        sys.exit(1)
    
    gdf = gpd.read_file(CHEMIN_RESEAU)
    if gdf.crs is None:
        gdf.set_crs("EPSG:32198", inplace=True)
    elif gdf.crs.to_epsg() != 32198:
        gdf = gdf.to_crs("EPSG:32198")
    print(f"  Réseau : {len(gdf):,} tronçons")
    
    etiages = pd.read_csv(CHEMIN_ETIAGES, encoding="utf-8-sig")
    etiages["station_norm"] = etiages["station"].astype(str).str.split("_").str[0].apply(normalize_code)
    print(f"  Étiages : {len(etiages)} stations")
    
    return gdf, etiages


def construire_stations(gdf_reseau, df_etiages):
    """
    Stations à analyser : celles présentes dans le réseau ET avec Q2,7 connu.
    On lit STATION du réseau pour identifier les tronçons.
    """
    print("\n" + "=" * 68)
    print("ÉTAPE 4 — Construction de la liste des stations analysables")
    print("=" * 68)
    
    # Mapping station → tronçon
    tronc_par_station = {}
    for idx, row in gdf_reseau.iterrows():
        s = row.get("STATION")
        if not isinstance(s, str) or s.strip() in ("", "-"):
            continue
        for code in s.replace(" ", "").split(","):
            if code and code != "-":
                tronc_par_station[normalize_code(code)] = idx
    print(f"  Stations dans le réseau : {len(tronc_par_station)}")
    
    # Construction
    rows = []
    for _, et in df_etiages.iterrows():
        code = et["station_norm"]
        idx_t = tronc_par_station.get(code)
        if idx_t is None:
            continue
        tr = gdf_reseau.loc[idx_t]
        if pd.isna(tr["BV_PRIM"]) or pd.isna(tr["SUPERFI"]):
            continue
        if pd.isna(et["ete_Q2_7"]):
            continue
        rows.append({
            "station": str(et["station"]),
            "station_norm": code,
            "nom": et["nom"],
            "bv_prim": tr["BV_PRIM"],
            "superficie_km2": float(tr["SUPERFI"]),
            "q27_ete_m3s": float(et["ete_Q2_7"]),
            "q27_hiv_m3s": safe_num(et["hiver_Q2_7"]),
            "geometry_troncon": tr["geometry"],
        })
    
    df = pd.DataFrame(rows)
    print(f"  Stations analysables (réseau + Q2,7) : {len(df)}")
    return df


# ============================================================
# ÉTAPE 5 — Snap des sites sur le réseau (BV_PRIM + SUPERFI)
# ============================================================

def snap_sites_sur_reseau(df_sites, gdf_reseau):
    """Snap chaque site au tronçon le plus proche, récupère BV_PRIM et SUPERFI."""
    print("\n" + "=" * 68)
    print("ÉTAPE 5 — Snap des sites sur le réseau hydrographique")
    print("=" * 68)
    print("  (peut prendre 1-3 minutes)")
    
    gdf_sites = gpd.GeoDataFrame(
        df_sites,
        geometry=gpd.points_from_xy(df_sites.longitude, df_sites.latitude),
        crs="EPSG:4326",
    ).to_crs("EPSG:32198")
    
    snap = gpd.sjoin_nearest(
        gdf_sites,
        gdf_reseau[["IDTRONC", "BV_PRIM", "SUPERFI", "geometry"]],
        how="left",
        max_distance=DIST_MAX_SNAP_M,
        distance_col="dist_snap_m",
    )
    # Conserver le tronçon le plus aval en cas d'ambiguïté
    snap = snap.sort_values(["Numéro du prélèvement", "SUPERFI"], ascending=[True, False])
    snap = snap.drop_duplicates("Numéro du prélèvement", keep="first")
    
    n_ok = snap["IDTRONC"].notna().sum()
    print(f"  Sites snapés : {n_ok}/{len(snap)} ({n_ok/len(snap)*100:.1f} %)")
    
    snap = snap[snap["IDTRONC"].notna()].copy()
    return snap


# ============================================================
# ÉTAPE 6 — Calcul de pression par station + détail intervenants
# ============================================================

def calculer_pression_et_details(df_stations, df_sites_snapes):
    """
    Pour chaque station, identifie ses préleveurs amont et calcule la pression.
    Retourne deux DataFrames : pression et détails.
    """
    print("\n" + "=" * 68)
    print("ÉTAPE 6 — Calcul de la pression et détail des intervenants")
    print("=" * 68)
    
    pression_rows = []
    details_rows = []
    
    for _, st in df_stations.iterrows():
        # Filtre 1 : même bassin primaire
        sites_bv = df_sites_snapes[df_sites_snapes["BV_PRIM"] == st["bv_prim"]]
        # Filtre 2 : SUPERFI plus petite (= en amont)
        sites_amont = sites_bv[sites_bv["SUPERFI"] <= st["superficie_km2"]].copy()
        
        debit_amont_total = sites_amont["debit_estival_m3s"].sum()
        q27 = st["q27_ete_m3s"]
        
        # Pression en étiage (la pression "actuelle" sera recalculée live par generate_state.py)
        pression_etiage = (
            (debit_amont_total / (q27 + debit_amont_total) * 100)
            if (q27 + debit_amont_total) > 0 else None
        )
        
        pression_rows.append({
            "station": st["station"],
            "nom": st["nom"],
            "plan_deau": st["nom"],   # le frontend utilisera ça si pas de plan_deau live
            "bv_prim": st["bv_prim"],
            "superfi_km2": st["superficie_km2"],
            "n_sites_amont": len(sites_amont),
            # debit_obs sera complété par l'API live (placeholder ici)
            "debit_obs_m3s": None,
            "debit_preleve_m3s": debit_amont_total,
            "debit_naturel_m3s": None,
            "q27_ete_m3s": q27,
            "pression_observe_pct": None,    # recalculé par generate_state.py avec live
            "pression_etiage_pct": pression_etiage,
        })
        
        # Détails : top N intervenants par station
        sites_amont_sorted = sites_amont.sort_values("debit_estival_m3s", ascending=False)
        top_n = sites_amont_sorted.head(TOP_N_INTERVENANTS)
        n_autres = len(sites_amont_sorted) - len(top_n)
        debit_autres = (
            sites_amont_sorted.iloc[TOP_N_INTERVENANTS:]["debit_estival_m3s"].sum()
            if n_autres > 0 else 0
        )
        
        for _, site in top_n.iterrows():
            details_rows.append({
                "station": st["station"],
                "nom_intervenant": site["intervenant"],
                "num_site": site["Numéro du prélèvement"],
                "secteur_scian": site["secteur_scian"],
                "municipalite": site["municipalite"],
                "debit_estival_m3s": site["debit_estival_m3s"],
                "volume_annuel_moyen_Mm3": site["volume_annuel_moyen_Mm3"],
                "premiere_annee": int(site["premiere_annee"]) if pd.notna(site["premiere_annee"]) else None,
                "derniere_annee": int(site["derniere_annee"]) if pd.notna(site["derniere_annee"]) else None,
                "rang": None,  # sera ajouté en sortie
            })
        
        # Ligne récapitulative pour les autres
        if n_autres > 0:
            details_rows.append({
                "station": st["station"],
                "nom_intervenant": f"+ {n_autres} autres préleveurs",
                "num_site": None,
                "secteur_scian": None,
                "municipalite": None,
                "debit_estival_m3s": debit_autres,
                "volume_annuel_moyen_Mm3": None,
                "premiere_annee": None,
                "derniere_annee": None,
                "rang": None,
            })
    
    df_pression = pd.DataFrame(pression_rows)
    df_details = pd.DataFrame(details_rows)
    
    # Ajouter le rang (numéro d'ordre par station)
    df_details["rang"] = df_details.groupby("station").cumcount() + 1
    
    n_avec_amont = (df_pression["n_sites_amont"] > 0).sum()
    print(f"  Stations avec préleveurs amont : {n_avec_amont}/{len(df_pression)}")
    print(f"  Lignes de détail produites : {len(df_details):,}")
    
    return df_pression, df_details


# ============================================================
# MAIN
# ============================================================

def main():
    t_start = time.time()
    print("\n🌊 HydroPression Québec — Reconstruction des données")
    print(f"   Méthode : moyenne 5 ans (2020-2024), saison estivale juin-octobre\n")
    
    df_decl = charger_prelevements_5ans()
    df_sites = calculer_profil_sites(df_decl)
    gdf_reseau, df_etiages = charger_reseau_et_stations()
    df_stations = construire_stations(gdf_reseau, df_etiages)
    df_sites_snapes = snap_sites_sur_reseau(df_sites, gdf_reseau)
    df_pression, df_details = calculer_pression_et_details(df_stations, df_sites_snapes)
    
    # Sauvegarder
    OUT_PRESSION.parent.mkdir(parents=True, exist_ok=True)
    df_pression.to_csv(OUT_PRESSION, index=False, encoding="utf-8-sig")
    df_details.to_csv(OUT_DETAILS, index=False, encoding="utf-8-sig")
    
    print("\n" + "=" * 68)
    print("✅ TERMINÉ")
    print("=" * 68)
    print(f"  {OUT_PRESSION.name} : {len(df_pression)} stations")
    print(f"  {OUT_DETAILS.name}   : {len(df_details):,} lignes")
    print(f"  Durée totale : {time.time()-t_start:.1f}s")
    print()
    print("Prochaines étapes :")
    print("  1. python enrich_csv_with_coords.py    (rajouter lat/lon)")
    print("  2. python generate_state.py            (régénérer le JSON)")
    print("  3. Recharger l'app web pour voir le résultat")


if __name__ == "__main__":
    main()
