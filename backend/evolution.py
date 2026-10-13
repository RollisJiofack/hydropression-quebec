#!/usr/bin/env python3
"""Annual evolution, separate from all legacy pressure/profile formulas.

python -m backend.evolution --station-csv BASELINE --etiages-csv ETIAGES
verifies in memory only. Add --write --expected-station-sha256 HASH to write
only the three new CSVs and identical copies for GitHub Pages in web/data.
Facts/dimension cover filtered surface rows; published measures must use
distinct sites in the correspondence, not a Quebec-wide total.
"""

import argparse
import calendar
import hashlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

SITE = "Num\u00e9ro du pr\u00e9l\u00e8vement"
YEAR = "Ann\u00e9e"
MONTH = "Mois"
DAYS = "Nombre de jours/mois"
GROSS = "Volume ventil\u00e9 par code SCIAN par site (L)"
EXPORT_NAMES = ("sites.csv", "sites_annees.csv", "correspondance_sites_stations.csv")
ANNUAL_COLUMNS = [
    "num_site", "annee", "mois_declares", "annee_partielle", "volume_brut_m3",
    "debit_brut_annuel_m3s", "debit_brut_juilaout_m3s", "conso_nette_annuel_m3s",
    "conso_nette_juilaout_m3s", "conso_defaut100_annuel_m3s",
]


@dataclass(frozen=True)
class EvolutionTables:
    sites: pd.DataFrame
    sites_annees: pd.DataFrame
    correspondance: pd.DataFrame


def _require_columns(df, columns):
    missing = set(columns) - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")


def _unique(df, keys):
    _require_columns(df, keys)
    if df[keys].isna().any().any() or df.duplicated(keys).any():
        raise ValueError(f"Missing or duplicate key: {keys}")


def _site_ids(series):
    def convert(value):
        if pd.isna(value):
            raise ValueError("Missing site identifier")
        if isinstance(value, (int, float, np.integer, np.floating)):
            if not np.isfinite(value) or value != int(value):
                raise ValueError(f"Invalid numeric site identifier: {value}")
            return str(int(value))
        value = str(value).strip()
        if not value:
            raise ValueError("Empty site identifier")
        return value
    return series.map(convert)


def _declarations(df_decl, annee_min, annee_max):
    if annee_max < annee_min:
        raise ValueError("Invalid year range")
    _require_columns(df_decl, [SITE, YEAR, MONTH, DAYS, GROSS,
                               "vol_consomme_L", "source_consommation"])
    df = df_decl.copy()
    years = pd.to_numeric(df[YEAR], errors="raise")
    df = df.loc[years.between(annee_min, annee_max)].copy()
    # Legacy site groupbys drop null identifiers. Keep that same identifiable
    # source scope, after (not before) the existing net-consumption allocation.
    df = df.loc[df[SITE].notna()].copy()
    df["num_site"] = _site_ids(df[SITE])
    for source, target in ((YEAR, "annee"), (MONTH, "mois")):
        values = pd.to_numeric(df[source], errors="raise")
        if values.isna().any() or (values % 1 != 0).any():
            raise ValueError(f"Invalid {source}")
        df[target] = values.astype(int)
    if not df["mois"].between(1, 12).all():
        raise ValueError("Invalid month")
    for column in (GROSS, "vol_consomme_L"):
        df[column] = pd.to_numeric(df[column], errors="raise")
        if not np.isfinite(df[column]).all():
            raise ValueError(f"Non-finite volume: {column}")
    df["jours"] = pd.to_numeric(df[DAYS], errors="raise").fillna(0)
    if not np.isfinite(df["jours"]).all():
        raise ValueError("Non-finite declared days")
    df["vol_defaut_L"] = df["vol_consomme_L"].where(
        df["source_consommation"].eq("4_defaut_100pct"), 0.0
    )
    return df


def agreger_mois(df_decl, annee_min=2020, annee_max=2024):
    """Sum SCIAN/place rows BEFORE division, using maximum declared days."""
    df = _declarations(df_decl, annee_min, annee_max)
    months = df.groupby(["num_site", "annee", "mois"], as_index=False).agg(
        volume_brut_L=(GROSS, "sum"), volume_consomme_L=("vol_consomme_L", "sum"),
        volume_defaut_L=("vol_defaut_L", "sum"), jours_declares=("jours", "max"),
    )
    invalid = months["jours_declares"].le(0) & (
        months["volume_brut_L"].ne(0) | months["volume_consomme_L"].ne(0)
    )
    if invalid.any():
        raise ValueError("Nonzero monthly volume without positive declared days")
    denominator = months["jours_declares"].where(months["jours_declares"].gt(0)) * 86400 * 1000
    for volume, flow in (("volume_brut_L", "debit_brut_m3s"),
                         ("volume_consomme_L", "conso_nette_m3s")):
        months[flow] = (months[volume] / denominator).fillna(0.0)
    return months


def construire_sites_annees(months):
    annual = months.groupby(["num_site", "annee"], as_index=False).agg(
        mois_declares=("mois", "nunique"), volume_brut_L=("volume_brut_L", "sum"),
        volume_consomme_L=("volume_consomme_L", "sum"), volume_defaut_L=("volume_defaut_L", "sum"),
    )
    annual["annee_partielle"] = annual["mois_declares"].lt(12)
    annual["volume_brut_m3"] = annual["volume_brut_L"] / 1000
    seconds = annual["annee"].map(lambda y: (366 if calendar.isleap(y) else 365) * 86400)
    for volume, flow in (("volume_brut_L", "debit_brut_annuel_m3s"),
                         ("volume_consomme_L", "conso_nette_annuel_m3s"),
                         ("volume_defaut_L", "conso_defaut100_annuel_m3s")):
        annual[flow] = annual[volume] / 1000 / seconds
    summer = months.loc[months["mois"].isin([7, 8])].groupby(
        ["num_site", "annee"], as_index=False
    ).agg(debit_brut_juilaout_m3s=("debit_brut_m3s", "max"),
          conso_nette_juilaout_m3s=("conso_nette_m3s", "max"))
    annual = annual.merge(summer, on=["num_site", "annee"], how="left", validate="one_to_one")
    return annual[ANNUAL_COLUMNS].sort_values(["num_site", "annee"]).reset_index(drop=True)


def _snap_unique(df_sites_snapes):
    columns = [SITE, "IDTRONC", "BV_PRIM", "SUPERFI", "debit_etiage_m3s"]
    _require_columns(df_sites_snapes, columns)
    snap = pd.DataFrame(df_sites_snapes[columns]).copy()
    snap["num_site"] = _site_ids(snap[SITE])
    snap = snap.drop(columns=SITE).drop_duplicates()
    _unique(snap, ["num_site"])
    return snap


def construire_correspondance(df_stations, df_sites_snapes):
    _require_columns(df_stations, ["station", "bv_prim", "superficie_km2"])
    _unique(df_stations, ["station"])
    snap = _snap_unique(df_sites_snapes)
    rows = []
    for _, station in df_stations.iterrows():
        # Published uncharacterized zeros do not mean physical absence of sites.
        if "prelevements_caracterises" in df_stations and station["prelevements_caracterises"] != 1:
            continue
        upstream = snap.loc[snap["BV_PRIM"].eq(station["bv_prim"])
                            & snap["SUPERFI"].le(station["superficie_km2"])]
        for _, site in upstream.iterrows():
            rows.append({"station": station["station"], "num_site": site["num_site"],
                         "en_etiage": bool(site["debit_etiage_m3s"] > 0)})
    return pd.DataFrame(rows, columns=["station", "num_site", "en_etiage"]).sort_values(
        ["station", "num_site"]
    ).reset_index(drop=True)


def construire_sites(df_decl, annual, df_sites_snapes, annee_min, annee_max):
    df = _declarations(df_decl, annee_min, annee_max)
    name_columns = [c for c in df if "intervenant" in c.lower() and "Nom" in c]
    if not name_columns:
        raise ValueError("Missing intervenant name column")
    metadata = {"nom_intervenant": name_columns[0], "municipalite": "Municipalit\u00e9",
                "secteur_scian": "Description du code SCIAN",
                "code_scian": "Code SCIAN par site par mois"}
    _require_columns(df, metadata.values())
    sites = df.groupby("num_site", as_index=False).agg(
        **{target: (source, "first") for target, source in metadata.items()}
    )
    declared = annual.groupby("num_site", as_index=False).agg(
        premiere_annee=("annee", "min"), derniere_annee=("annee", "max"),
        nb_annees_declarees=("annee", "nunique"),
    )
    declared[f"panel_{annee_min}_{annee_max}"] = declared["nb_annees_declarees"].eq(
        annee_max - annee_min + 1
    )
    sites = sites.merge(declared, on="num_site", validate="one_to_one")
    snap = _snap_unique(df_sites_snapes).rename(columns={"IDTRONC": "idtronc", "BV_PRIM": "bv_prim"})
    return sites.merge(snap[["num_site", "idtronc", "bv_prim"]], on="num_site", how="left",
                       validate="one_to_one").sort_values("num_site").reset_index(drop=True)


def valider_exports(tables, df_decl, df_stations, df_pression, annee_min=2020, annee_max=2024):
    """Stop before writing on duplicate keys, wrong coverage or annual totals."""
    sites, annual, corr = tables.sites, tables.sites_annees, tables.correspondance
    for frame, keys in ((sites, ["num_site"]), (annual, ["num_site", "annee"]),
                        (corr, ["station", "num_site"]), (df_pression, ["station"]),
                        (df_stations, ["station"])):
        _unique(frame, keys)
    _require_columns(df_pression, ["n_sites_amont"])
    if set(df_stations["station"]) != set(df_pression["station"]):
        raise ValueError("Station universe differs from the authoritative baseline")
    if set(annual["num_site"]) != set(sites["num_site"]):
        raise ValueError("Site dimension and annual facts differ")
    if not set(corr["num_site"]).issubset(set(sites["num_site"])):
        raise ValueError("Correspondence refers to absent sites")
    if not set(corr["station"]).issubset(set(df_pression["station"])):
        raise ValueError("Correspondence refers to absent stations")
    expected = pd.to_numeric(df_pression.set_index("station")["n_sites_amont"], errors="raise")
    if expected.isna().any() or (expected < 0).any() or (expected % 1 != 0).any():
        raise ValueError("Invalid baseline n_sites_amont")
    actual = corr.groupby("station").size().reindex(expected.index, fill_value=0)
    mismatch = actual.ne(expected)
    if mismatch.any():
        comparison = pd.DataFrame({"baseline": expected, "evolution": actual})
        raise ValueError("Upstream coverage mismatch:\n" + comparison.loc[mismatch].to_string())
    source = _declarations(df_decl, annee_min, annee_max)
    if set(source["num_site"]) != set(sites["num_site"]):
        raise ValueError("Filtered source and site dimension differ")
    declared_months = source.groupby(["num_site", "annee"])["mois"].nunique()
    fact_index = pd.MultiIndex.from_frame(annual[["num_site", "annee"]])
    if set(fact_index) != set(declared_months.index):
        raise ValueError("Annual keys differ from declared source site-years")
    if not np.array_equal(annual["mois_declares"], declared_months.reindex(fact_index)):
        raise ValueError("Declared month count differs from source")
    panel_column = f"panel_{annee_min}_{annee_max}"
    expected_panel = source.groupby("num_site")["annee"].nunique().eq(annee_max - annee_min + 1)
    if not np.array_equal(sites[panel_column], expected_panel.reindex(sites["num_site"])):
        raise ValueError("Constant panel differs from source declaration years")
    if not annual["mois_declares"].between(1, 12).all():
        raise ValueError("Invalid declared month count")
    if not annual["annee_partielle"].eq(annual["mois_declares"].lt(12)).all():
        raise ValueError("Invalid partial-year flag")
    years = pd.Index(range(annee_min, annee_max + 1), name="annee")
    # Validate raw and published scope separately, deduplicating shared sites.
    for scope in (None, set(corr["num_site"])):
        raw = source if scope is None else source.loc[source["num_site"].isin(scope)]
        facts = annual if scope is None else annual.loc[annual["num_site"].isin(scope)]
        seconds = facts["annee"].map(lambda y: (366 if calendar.isleap(y) else 365) * 86400)
        for column, fact in ((GROSS, "volume_brut_m3"),
                             ("vol_consomme_L", "conso_nette_annuel_m3s"),
                             ("vol_defaut_L", "conso_defaut100_annuel_m3s")):
            expected_total = raw.groupby("annee")[column].sum().reindex(years, fill_value=0) / 1000
            amounts = facts[fact] if fact == "volume_brut_m3" else facts[fact] * seconds
            actual_total = amounts.groupby(facts["annee"]).sum().reindex(years, fill_value=0)
            if not np.allclose(actual_total, expected_total, rtol=1e-12, atol=1e-8):
                raise ValueError(f"Annual source totals mismatch: {column}, published={scope is not None}")


def construire_exports(df_decl, df_sites_snapes, df_stations, df_pression,
                        annee_min=2020, annee_max=2024):
    annual = construire_sites_annees(agreger_mois(df_decl, annee_min, annee_max))
    sites = construire_sites(df_decl, annual, df_sites_snapes, annee_min, annee_max)
    corr = construire_correspondance(df_stations, df_sites_snapes)
    tables = EvolutionTables(sites, annual, corr)
    valider_exports(tables, df_decl, df_stations, df_pression, annee_min, annee_max)
    return tables


def auditer_agregation(df_decl, annee_min=2020, annee_max=2024):
    """Explain differences from legacy row-mean flows without modifying them."""
    source = _declarations(df_decl, annee_min, annee_max)
    _require_columns(source, ["debit_consomme_jour_m3s"])
    keys = ["num_site", "annee", "mois"]
    old = source.groupby(keys).agg(
        lignes=("mois", "size"), jours_distincts=("jours", "nunique"),
        debit_legacy=("debit_consomme_jour_m3s", "mean"),
    )
    new = agreger_mois(df_decl, annee_min, annee_max).set_index(keys)
    differences = ~np.isclose(old["debit_legacy"], new["conso_nette_m3s"], rtol=1e-12, atol=1e-15)
    if (differences & old["lignes"].eq(1)).any():
        raise ValueError("Unexplained difference from legacy single-row monthly flow")
    return {"multi_lignes": int(old["lignes"].gt(1).sum()),
            "jours_differents": int(old["jours_distincts"].gt(1).sum()),
            "debits_mensuels_differents": int(differences.sum())}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def empreintes_legacy(repo_root, extra_paths=()):
    paths = set(Path(repo_root).joinpath("backend", "data").rglob("*"))
    paths.update(Path(repo_root).joinpath("web", "data").rglob("*"))
    paths.update(Path(p) for p in extra_paths)
    return {p.resolve(): sha256(p) for p in paths if p.is_file() and p.name not in EXPORT_NAMES}


def verifier_empreintes(before):
    changed = [str(p) for p, digest in before.items() if not p.is_file() or sha256(p) != digest]
    if changed:
        raise ValueError(f"Legacy/source hashes changed: {changed}")


def ecrire_exports(tables, data_dir, web_data_dir, before=None):
    """Stage/copy only new exports; never copy legacy pressure files."""
    before = before or {}
    verifier_empreintes(before)
    data_dir, web_data_dir = Path(data_dir), Path(web_data_dir)
    with TemporaryDirectory(prefix=".evolution-stage-", dir=data_dir.parent) as temp:
        for name, frame in zip(EXPORT_NAMES, (tables.sites, tables.sites_annees, tables.correspondance)):
            frame.to_csv(Path(temp, name), index=False, encoding="utf-8-sig")
        verifier_empreintes(before)
        for directory in (data_dir, web_data_dir):
            directory.mkdir(parents=True, exist_ok=True)
            for name in EXPORT_NAMES:
                target = directory / name
                if target.is_symlink() or target.resolve().parent != directory.resolve():
                    raise ValueError(f"Unsafe export target: {target}")
            with TemporaryDirectory(prefix=".evolution-", dir=directory) as local:
                for name in EXPORT_NAMES:
                    Path(local, name).write_bytes(Path(temp, name).read_bytes())
                verifier_empreintes(before)
                for name in EXPORT_NAMES:
                    Path(local, name).replace(directory / name)
    verifier_empreintes(before)
    for name in EXPORT_NAMES:
        if sha256(data_dir / name) != sha256(web_data_dir / name):
            raise ValueError(f"Published copy differs: {name}")


def generer(station_csv, etiages_csv=None, *, write=False, expected_station_sha256=None, pipeline=None):
    """Reuse loader/consumption/profile/snap functions, never legacy main/writers."""
    station_csv = Path(station_csv).resolve()
    digest = sha256(station_csv)
    if expected_station_sha256 is not None and digest != expected_station_sha256.lower():
        raise ValueError("Station CSV hash does not match the approved baseline")
    if write and expected_station_sha256 is None:
        raise ValueError("Writing requires --expected-station-sha256")
    baseline = pd.read_csv(station_csv, encoding="utf-8-sig", dtype={"station": str})
    _require_columns(baseline, ["station", "bv_prim", "superfi_km2", "n_sites_amont",
                                "prelevements_caracterises"])
    _unique(baseline, ["station"])
    if not baseline["prelevements_caracterises"].isin([0, 1]).all():
        raise ValueError("Invalid characterization flag")
    skipped = baseline["prelevements_caracterises"].eq(0).sum()
    print(f"Baseline: {len(baseline)} stations; SHA256={digest}")
    print(f"Characterized: {len(baseline) - skipped}; uncharacterized skipped: {skipped}")
    print("Uncharacterized zeros are not evidence of physical absence of upstream sites.")
    if pipeline is None:
        if __package__:
            from . import build_details as pipeline
        else:
            import build_details as pipeline
    repo_root = pipeline.ROOT.parent
    extra = [station_csv, pipeline.CHEMIN_EXCEL, pipeline.CHEMIN_COEFFICIENTS,
             etiages_csv if etiages_csv is not None else pipeline.CHEMIN_ETIAGES]
    before = empreintes_legacy(repo_root, extra)
    original_etiages = pipeline.CHEMIN_ETIAGES
    try:
        if etiages_csv is not None:
            pipeline.CHEMIN_ETIAGES = Path(etiages_csv).resolve()
        official, empirical = pipeline.charger_coefficients()
        declarations = pipeline.charger_prelevements_5ans()
        required_years = set(range(pipeline.ANNEE_MIN, pipeline.ANNEE_MAX + 1))
        if set(declarations["_annee_source"].unique()) != required_years:
            raise ValueError("Not all annual source sheets were successfully loaded")
        declarations = pipeline.calculer_consommation(declarations, official, empirical)
        unidentified = declarations.loc[declarations[SITE].isna()]
        print(f"Unidentifiable rows excluded by legacy site groupbys: {len(unidentified)}; "
              f"gross={unidentified[GROSS].sum() / 1000:.6f} m3; "
              f"net={unidentified['vol_consomme_L'].sum() / 1000:.6f} m3")
        retained_sites = pipeline.calculer_profil_mensuel(declarations)
        network, _ = pipeline.charger_reseau_et_stations()
        snapped_sites = pipeline.snap_sites_sur_reseau(retained_sites, network)
        stations = baseline.rename(columns={"superfi_km2": "superficie_km2"})
        tables = pipeline.exporter_evolution(declarations, snapped_sites, stations, baseline)
        aggregation_audit = auditer_agregation(declarations, pipeline.ANNEE_MIN, pipeline.ANNEE_MAX)
        print(f"Monthly aggregation audit: {aggregation_audit}; legacy profile/pressures unchanged.")
        print(f"Validated: {len(tables.sites)} sites; {len(tables.sites_annees)} site-years; "
              f"{len(tables.correspondance)} station-site pairs")
        published = set(tables.correspondance["num_site"])
        print(f"Published scope: {len(published)} distinct upstream sites (not a Quebec total)")
        for year, rows in tables.sites_annees.groupby("annee"):
            scope = rows.loc[rows["num_site"].isin(published)]
            print(f"  {year}: {len(rows)} declared sites; {len(scope)} upstream; "
                  f"{scope['annee_partielle'].sum()} partial upstream site-years; "
                  f"{scope['volume_brut_m3'].sum():.6f} gross m3 upstream")
        verifier_empreintes(before)
        if write:
            ecrire_exports(tables, pipeline.ROOT / "data", repo_root / "web" / "data", before)
            print("Wrote only three evolution CSVs and their identical Pages copies.")
        else:
            print("Read-only verification: no files written.")
        return tables
    finally:
        pipeline.CHEMIN_ETIAGES = original_etiages
        verifier_empreintes(before)
        print(f"Legacy/source SHA256 hashes unchanged: {len(before)} files")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-csv", type=Path,
                        default=Path(__file__).resolve().parent / "data" / "resultats_pression_phase2.csv")
    parser.add_argument("--etiages-csv", type=Path, help="Read-only etiage baseline override")
    parser.add_argument("--expected-station-sha256", help="SHA256 of the approved station CSV")
    parser.add_argument("--write", action="store_true", help="Write only new exports and Pages copies")
    args = parser.parse_args(argv)
    if args.write and not args.expected_station_sha256:
        parser.error("--write requires --expected-station-sha256")
    generer(args.station_csv, args.etiages_csv, write=args.write,
            expected_station_sha256=args.expected_station_sha256)


if __name__ == "__main__":
    main()
