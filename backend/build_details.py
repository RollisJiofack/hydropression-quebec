#!/usr/bin/env python3
"""
build_details.py — HydroPression Québec (v2 — débits mensuels)

Reconstruit deux fichiers à partir du fichier Excel des déclarations :

1. resultats_pression_phase2.csv  — pression d'étiage par station + 12 colonnes
                                    de débits mensuels prélevés
2. details_intervenants.csv       — détail des préleveurs avec 12 valeurs
                                    mensuelles de débit par préleveur

Méthode :
- Période d'analyse : 2020-2024 (5 dernières années).
- Pour CHAQUE MOIS (1-12) et chaque site :
    débit_mois = moyenne arithmétique sur les années où le site a déclaré
                 ce mois-là (volume_mois / jours_mois / 86400 / 1000).
    Mois sans aucune déclaration sur 5 ans → débit = 0.
- Pour la pression d'étiage estival :
    débit_etiage = MAX(débit_juillet, débit_aout) par site.
    Sites sans aucune déclaration en juillet et août sur 5 ans → exclus.
- Filtre Surface uniquement (exclut Souterrain et Aqueduc).

Cas particuliers :
- volume = 0, jours > 0 : débit = 0 (préleveur actif sans pompage)
- volume > 0, jours = 0 : ligne ignorée (donnée erronée)

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

CHEMIN_EXCEL = Path(r"C:\Users\jioro01\Desktop\pression-eau-quebec\prelevements-eau-declares-depuis-2012.xlsx")
CHEMIN_RESEAU = Path(r"C:\Users\jioro01\Desktop\pression-eau-quebec\Atlas de l'eau\AtlasH2020_EA_HP.shp")

ROOT = Path(__file__).resolve().parent
CHEMIN_ETIAGES = ROOT / "data" / "debits_etiage_cehq.csv"

OUT_PRESSION = ROOT / "data" / "resultats_pression_phase2.csv"
OUT_DETAILS = ROOT / "data" / "details_intervenants.csv"

# Paramètres méthodologiques
ANNEE_MIN = 2020
ANNEE_MAX = 2024
MOIS_ETIAGE = (7, 8)            # MAX(juillet, août) pour la pression d'étiage
DIST_MAX_SNAP_M = 2000

TOP_N_INTERVENANTS = 20


# ============================================================
# UTILITAIRES
# ============================================================

def normalize_code(code) -> str:
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
# ÉTAPE 1 — Charger les déclarations 2020-2024
# ============================================================

def charger_prelevements_5ans():
    """
    Charge les onglets 2020-2024, filtre Surface, calcule un débit
    journalier par ligne (mois × site × année).
    """
    print("=" * 68)
    print("ÉTAPE 1 — Chargement des déclarations 2020-2024")
    print("=" * 68)

    if not CHEMIN_EXCEL.exists():
        print(f"❌ Fichier Excel introuvable : {CHEMIN_EXCEL}")
        sys.exit(1)

    annees = list(range(ANNEE_MIN, ANNEE_MAX + 1))
    dfs = []
    for an in annees:
        try:
            t0 = time.time()
            df = pd.read_excel(CHEMIN_EXCEL, sheet_name=str(an))
            df["_annee_source"] = an
            print(f"  {an} : {len(df):>7,} lignes en {time.time()-t0:.1f}s")
            dfs.append(df)
        except Exception as e:
            print(f"  {an} : ⚠️  non chargé ({e})")

    df = pd.concat(dfs, ignore_index=True)
    df = df.replace("NULL", np.nan)

    cols_num = [
        "Longitude (site)", "Latitude (site)",
        "Volume ventilé par code SCIAN par site (L)",
        "Nombre de jours/mois", "Année", "Mois",
    ]
    for c in cols_num:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    print(f"\n  Total brut : {len(df):,}")

    # Filtre Surface uniquement
    df = df[df["Source du prélèvement"] == "Surface"].copy()
    print(f"  Surface : {len(df):,}")

    # Coordonnées valides
    df = df.dropna(subset=["Longitude (site)", "Latitude (site)"])
    print(f"  Avec coords : {len(df):,}")

    # Mois valide (1-12)
    df = df[df["Mois"].between(1, 12)]
    print(f"  Mois valide : {len(df):,}")

    # Cas particuliers :
    # - volume > 0 et jours = 0 → erreur de saisie, on ignore
    # - volume = 0 et jours > 0 → débit = 0, on garde
    vol_pos = df["Volume ventilé par code SCIAN par site (L)"] > 0
    jrs_zero = (df["Nombre de jours/mois"].isna()) | (df["Nombre de jours/mois"] == 0)
    erreurs = vol_pos & jrs_zero
    print(f"  Lignes erronées (vol>0 mais jours=0) : {erreurs.sum():,} → ignorées")
    df = df[~erreurs]

    # Calcul débit journalier par ligne
    df["debit_jour_m3s"] = np.where(
        df["Nombre de jours/mois"] > 0,
        df["Volume ventilé par code SCIAN par site (L)"]
            / df["Nombre de jours/mois"] / 86400 / 1000,
        0.0
    )
    df["debit_jour_m3s"] = df["debit_jour_m3s"].fillna(0)

    # Filtre final : volume non-null
    df = df[df["Volume ventilé par code SCIAN par site (L)"].notna()]
    print(f"  Lignes finales : {len(df):,}")

    return df


# ============================================================
# ÉTAPE 2 — Profil mensuel sur 5 ans par site
# ============================================================

def calculer_profil_mensuel(df_decl):
    """
    Pour chaque (site × mois) : moyenne du débit journalier sur les années
    où le site a déclaré ce mois-là.
    Retourne un DataFrame avec une colonne par mois (debit_mois_01 à 12).
    """
    print("\n" + "=" * 68)
    print("ÉTAPE 2 — Profil mensuel par site (moyenne 5 ans)")
    print("=" * 68)

    # Étape 2a : agréger par (site × mois × année) — un débit par mois × année
    par_an = df_decl.groupby(
        ["Numéro du prélèvement", "Mois", "Année"]
    )["debit_jour_m3s"].mean().reset_index()

    # Étape 2b : moyenne sur les années pour chaque (site × mois)
    profil = par_an.groupby(
        ["Numéro du prélèvement", "Mois"]
    )["debit_jour_m3s"].mean().reset_index()

    # Étape 2c : pivoter pour avoir 12 colonnes (1 par mois)
    pivot = profil.pivot(
        index="Numéro du prélèvement",
        columns="Mois",
        values="debit_jour_m3s"
    )

    # S'assurer que les 12 colonnes existent (1-12), avec 0 pour mois jamais déclarés
    for mois in range(1, 13):
        if mois not in pivot.columns:
            pivot[mois] = 0.0
    pivot = pivot[[m for m in range(1, 13)]]   # ordre garanti
    pivot = pivot.fillna(0.0)
    pivot.columns = [f"debit_mois_{int(m):02d}_m3s" for m in pivot.columns]

    # Étape 2d : MAX(juillet, août) — débit utilisé pour la pression d'étiage
    pivot["debit_etiage_m3s"] = pivot[
        [f"debit_mois_{m:02d}_m3s" for m in MOIS_ETIAGE]
    ].max(axis=1)

    # Métadonnées par site
    col_int = [c for c in df_decl.columns if "intervenant" in c.lower() and "Nom" in c][0]
    meta = df_decl.groupby("Numéro du prélèvement").agg(
        longitude=("Longitude (site)", "first"),
        latitude=("Latitude (site)", "first"),
        intervenant=(col_int, "first"),
        municipalite=("Municipalité", "first"),
        secteur_scian=("Description du code SCIAN", "first"),
        code_scian=("Code SCIAN par site par mois", "first"),
        premiere_annee=("Année", "min"),
        derniere_annee=("Année", "max"),
    )

    # Volume annuel moyen sur les années actives
    df_vol_an = df_decl.groupby(["Numéro du prélèvement", "Année"])[
        "Volume ventilé par code SCIAN par site (L)"
    ].sum().reset_index()
    vol_moyen = df_vol_an.groupby("Numéro du prélèvement")[
        "Volume ventilé par code SCIAN par site (L)"
    ].mean()
    meta["volume_annuel_moyen_Mm3"] = vol_moyen / 1e9

    sites = meta.join(pivot).reset_index()

    # Garder seulement les sites qui ont au moins une déclaration positive
    cols_mois = [f"debit_mois_{m:02d}_m3s" for m in range(1, 13)]
    a_des_donnees = (sites[cols_mois].sum(axis=1) > 0)
    sites = sites[a_des_donnees].copy()

    print(f"  Sites avec profil 5 ans : {len(sites):,}")
    print(f"  Sites avec activité estivale (juillet ou août) : "
          f"{(sites['debit_etiage_m3s'] > 0).sum():,}")
    print(f"\n  Total débit prélevé par mois :")
    for m in range(1, 13):
        col = f"debit_mois_{m:02d}_m3s"
        print(f"    Mois {m:02d} : {sites[col].sum():>7.2f} m³/s")

    return sites


# ============================================================
# ÉTAPE 3 — Réseau hydrographique et stations
# ============================================================

def charger_reseau_et_stations():
    print("\n" + "=" * 68)
    print("ÉTAPE 3 — Réseau hydrographique et débits d'étiage")
    print("=" * 68)

    if not CHEMIN_RESEAU.exists():
        print(f"❌ Réseau introuvable : {CHEMIN_RESEAU}")
        sys.exit(1)
    if not CHEMIN_ETIAGES.exists():
        print(f"❌ CSV étiages introuvable : {CHEMIN_ETIAGES}")
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
    print("\n" + "=" * 68)
    print("ÉTAPE 4 — Liste des stations analysables")
    print("=" * 68)

    tronc_par_station = {}
    for idx, row in gdf_reseau.iterrows():
        s = row.get("STATION")
        if not isinstance(s, str) or s.strip() in ("", "-"):
            continue
        for code in s.replace(" ", "").split(","):
            if code and code != "-":
                tronc_par_station[normalize_code(code)] = idx
    print(f"  Stations dans le réseau : {len(tronc_par_station)}")

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
    print(f"  Stations analysables : {len(df)}")
    return df


# ============================================================
# ÉTAPE 5 — Snap des sites sur le réseau
# ============================================================

def snap_sites_sur_reseau(df_sites, gdf_reseau):
    print("\n" + "=" * 68)
    print("ÉTAPE 5 — Snap des sites sur le réseau (1-3 min)")
    print("=" * 68)

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
    snap = snap.sort_values(["Numéro du prélèvement", "SUPERFI"], ascending=[True, False])
    snap = snap.drop_duplicates("Numéro du prélèvement", keep="first")

    n_ok = snap["IDTRONC"].notna().sum()
    print(f"  Sites snapés : {n_ok}/{len(snap)} ({n_ok/len(snap)*100:.1f} %)")

    return snap[snap["IDTRONC"].notna()].copy()


# ============================================================
# ÉTAPE 6 — Calcul de la pression et détails
# ============================================================

def calculer_pression_et_details(df_stations, df_sites_snapes):
    print("\n" + "=" * 68)
    print("ÉTAPE 6 — Pression par station et détails par préleveur")
    print("=" * 68)

    pression_rows = []
    details_rows = []

    for _, st in df_stations.iterrows():
        # Filtre 1 : même bassin primaire
        sites_bv = df_sites_snapes[df_sites_snapes["BV_PRIM"] == st["bv_prim"]]
        # Filtre 2 : SUPERFI plus petite (= en amont)
        sites_amont = sites_bv[sites_bv["SUPERFI"] <= st["superficie_km2"]].copy()

        # Pour pression d'étiage : seulement sites avec activité juillet/août
        sites_etiage = sites_amont[sites_amont["debit_etiage_m3s"] > 0].copy()
        debit_etiage_total = sites_etiage["debit_etiage_m3s"].sum()
        q27 = st["q27_ete_m3s"]
        pression_etiage = (
            (debit_etiage_total / (q27 + debit_etiage_total) * 100)
            if (q27 + debit_etiage_total) > 0 else None
        )

        # Pour chaque mois : somme des débits amont
        debits_mensuels_station = {}
        for mois in range(1, 13):
            col = f"debit_mois_{mois:02d}_m3s"
            debits_mensuels_station[mois] = sites_amont[col].sum()

        row = {
            "station": st["station"],
            "nom": st["nom"],
            "plan_deau": st["nom"],
            "bv_prim": st["bv_prim"],
            "superfi_km2": st["superficie_km2"],
            "n_sites_amont": len(sites_amont),
            "n_sites_etiage": len(sites_etiage),
            "debit_obs_m3s": None,
            "q27_ete_m3s": q27,
            "debit_preleve_etiage_m3s": debit_etiage_total,
            "pression_etiage_pct": pression_etiage,
        }
        # Ajouter les 12 colonnes mensuelles
        for mois in range(1, 13):
            row[f"debit_preleve_mois_{mois:02d}_m3s"] = debits_mensuels_station[mois]
        pression_rows.append(row)

        # Détails : top N préleveurs amont (tri par débit d'étiage)
        sorted_amont = sites_amont.sort_values("debit_etiage_m3s", ascending=False)
        top_n = sorted_amont.head(TOP_N_INTERVENANTS)
        n_autres = len(sorted_amont) - len(top_n)

        for rang, (_, site) in enumerate(top_n.iterrows(), start=1):
            d_row = {
                "station": st["station"],
                "rang": rang,
                "nom_intervenant": site["intervenant"],
                "num_site": site["Numéro du prélèvement"],
                "secteur_scian": site["secteur_scian"],
                "municipalite": site["municipalite"],
                "debit_etiage_m3s": site["debit_etiage_m3s"],
                "volume_annuel_moyen_Mm3": site["volume_annuel_moyen_Mm3"],
                "premiere_annee": int(site["premiere_annee"]) if pd.notna(site["premiere_annee"]) else None,
                "derniere_annee": int(site["derniere_annee"]) if pd.notna(site["derniere_annee"]) else None,
            }
            for mois in range(1, 13):
                d_row[f"debit_mois_{mois:02d}_m3s"] = site[f"debit_mois_{mois:02d}_m3s"]
            details_rows.append(d_row)

        # Ligne récapitulative pour les autres
        if n_autres > 0:
            autres = sorted_amont.iloc[TOP_N_INTERVENANTS:]
            d_row = {
                "station": st["station"],
                "rang": TOP_N_INTERVENANTS + 1,
                "nom_intervenant": f"+ {n_autres} autres préleveurs",
                "num_site": None,
                "secteur_scian": None,
                "municipalite": None,
                "debit_etiage_m3s": autres["debit_etiage_m3s"].sum(),
                "volume_annuel_moyen_Mm3": None,
                "premiere_annee": None,
                "derniere_annee": None,
            }
            for mois in range(1, 13):
                d_row[f"debit_mois_{mois:02d}_m3s"] = autres[f"debit_mois_{mois:02d}_m3s"].sum()
            details_rows.append(d_row)

    df_pression = pd.DataFrame(pression_rows)
    df_details = pd.DataFrame(details_rows)

    n_avec = (df_pression["n_sites_amont"] > 0).sum()
    print(f"  Stations avec préleveurs amont : {n_avec}/{len(df_pression)}")
    print(f"  Lignes de détail : {len(df_details):,}")
    return df_pression, df_details


# ============================================================
# MAIN
# ============================================================

def main():
    t_start = time.time()
    print("\n🌊 HydroPression Québec — Reconstruction des données (v2)")
    print("   Méthode : débits mensuels 5 ans + MAX(juillet, août) pour étiage\n")

    df_decl = charger_prelevements_5ans()
    df_sites = calculer_profil_mensuel(df_decl)
    gdf_reseau, df_etiages = charger_reseau_et_stations()
    df_stations = construire_stations(gdf_reseau, df_etiages)
    df_sites_snapes = snap_sites_sur_reseau(df_sites, gdf_reseau)
    df_pression, df_details = calculer_pression_et_details(df_stations, df_sites_snapes)

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
    print("  1. python enrich_csv_with_coords.py")
    print("  2. python generate_state.py")
    print("  3. Recharger l'app web")


if __name__ == "__main__":
    main()
