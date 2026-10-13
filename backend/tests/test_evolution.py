import contextlib
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from backend import evolution as e


def declaration(site="001", year=2020, month=7, gross=1000, consumed=300,
                days=10, source="1_donnee_declaree", scian=2213, place="place1"):
    return {e.SITE: site, e.YEAR: year, e.MONTH: month, e.DAYS: days, e.GROSS: gross,
            "vol_consomme_L": consumed, "source_consommation": source,
            "Nom de l'intervenant": "Operator", "Municipalit\u00e9": "Town",
            "Description du code SCIAN": "Sector", "Code SCIAN par site par mois": scian,
            "Num\u00e9ro du lieu": place, "_annee_source": year,
            "debit_consomme_jour_m3s": consumed / days / 86400 / 1000 if days > 0 else 0}


def snapped(site="001", area=10, basin="B", flow=1):
    return {e.SITE: site, "IDTRONC": "T1", "BV_PRIM": basin,
            "SUPERFI": area, "debit_etiage_m3s": flow}


def stations(count=1):
    return pd.DataFrame([
        {"station": "046709_08_01", "bv_prim": "B", "superficie_km2": 100,
         "n_sites_amont": count, "prelevements_caracterises": 1},
        {"station": "020802", "bv_prim": "other", "superficie_km2": 100,
         "n_sites_amont": 0, "prelevements_caracterises": 1},
        {"station": "000003", "bv_prim": "B", "superficie_km2": 100,
         "n_sites_amont": 0, "prelevements_caracterises": 0},
    ])


class AggregationTests(unittest.TestCase):
    def annual(self, rows):
        return e.construire_sites_annees(e.agreger_mois(pd.DataFrame(rows)))

    def test_multi_scian_and_places_sum_before_division(self):
        raw = pd.DataFrame([
            declaration(gross=1000, consumed=100, scian=1111),
            declaration(gross=2000, consumed=200, scian=2213, place="place2"),
        ])
        row = e.agreger_mois(raw).iloc[0]
        self.assertEqual(row.volume_brut_L, 3000)
        self.assertEqual(row.volume_consomme_L, 300)
        self.assertAlmostEqual(row.debit_brut_m3s, 3 / 10 / 86400)
        self.assertAlmostEqual(row.conso_nette_m3s, .3 / 10 / 86400)
        self.assertEqual(len(e.agreger_mois(raw)), 1)

    def test_different_days_use_max_not_sum_or_average_of_flows(self):
        row = e.agreger_mois(pd.DataFrame([
            declaration(gross=1000, consumed=200, days=5),
            declaration(gross=3000, consumed=600, days=20),
        ])).iloc[0]
        self.assertEqual(row.jours_declares, 20)
        self.assertAlmostEqual(row.debit_brut_m3s, 4 / 20 / 86400)
        self.assertAlmostEqual(row.conso_nette_m3s, .8 / 20 / 86400)

    def test_leap_years_and_calendar_annual_equivalents(self):
        annual = self.annual([declaration(year=y, gross=86400000, consumed=43200000)
                              for y in (2020, 2021, 2024)])
        for _, row in annual.iterrows():
            days = 365 if row.annee == 2021 else 366
            self.assertAlmostEqual(row.debit_brut_annuel_m3s, 1 / days)
            self.assertAlmostEqual(row.conso_nette_annuel_m3s, .5 / days)
            self.assertAlmostEqual(row.debit_brut_annuel_m3s * days * 86400, row.volume_brut_m3)

    def test_missing_july_and_august_are_missing_not_zero(self):
        row = self.annual([declaration(month=6)]).iloc[0]
        self.assertTrue(pd.isna(row.debit_brut_juilaout_m3s))
        self.assertTrue(pd.isna(row.conso_nette_juilaout_m3s))

    def test_one_declared_summer_month_zero_is_real_zero(self):
        row = self.annual([declaration(month=8, gross=0, consumed=0)]).iloc[0]
        self.assertEqual(row.debit_brut_juilaout_m3s, 0)
        self.assertEqual(row.conso_nette_juilaout_m3s, 0)
        self.assertEqual(row.mois_declares, 1)

    def test_summer_maximum_is_per_site_and_independent_for_net(self):
        row = self.annual([
            declaration(month=7, gross=10000, consumed=100, days=10),
            declaration(month=8, gross=5000, consumed=5000, days=10),
        ]).iloc[0]
        self.assertAlmostEqual(row.debit_brut_juilaout_m3s, 10 / 10 / 86400)
        self.assertAlmostEqual(row.conso_nette_juilaout_m3s, 5 / 10 / 86400)

    def test_declared_month_count_partial_year_no_imputation(self):
        annual = self.annual([declaration(month=2), declaration(month=2, scian=1111),
                              declaration(month=6)])
        row = annual.iloc[0]
        self.assertEqual(row.mois_declares, 2)
        self.assertTrue(row.annee_partielle)
        self.assertEqual(row.volume_brut_m3, 3)
        self.assertEqual(len(annual), 1)
        full = self.annual([declaration(month=m) for m in range(1, 13)]).iloc[0]
        self.assertEqual(full.mois_declares, 12)
        self.assertFalse(full.annee_partielle)

    def test_level_four_portion_reuses_exact_existing_allocation(self):
        row = self.annual([
            declaration(consumed=77, source="4_defaut_100pct"),
            declaration(consumed=23, source="2_officiel_RDPE"),
        ]).iloc[0]
        self.assertAlmostEqual(row.conso_nette_annuel_m3s, .1 / 366 / 86400)
        self.assertAlmostEqual(row.conso_defaut100_annuel_m3s, .077 / 366 / 86400)

    def test_zero_volume_zero_days_valid_nonzero_invalid(self):
        row = self.annual([declaration(gross=0, consumed=0, days=0)]).iloc[0]
        self.assertEqual(row.debit_brut_juilaout_m3s, 0)
        with self.assertRaisesRegex(ValueError, "declared days"):
            self.annual([declaration(days=0)])

    def test_years_outside_period_excluded(self):
        annual = self.annual([declaration(year=2019), declaration(year=2020),
                              declaration(year=2025)])
        self.assertEqual(annual.annee.tolist(), [2020])

    def test_legacy_differences_audited_without_changing_source(self):
        raw = pd.DataFrame([declaration(days=5), declaration(days=20)])
        before = raw.copy(deep=True)
        self.assertEqual(e.auditer_agregation(raw),
                         {"multi_lignes": 1, "jours_differents": 1, "debits_mensuels_differents": 1})
        pd.testing.assert_frame_equal(raw, before)


class TablesTests(unittest.TestCase):
    def setUp(self):
        self.raw = pd.DataFrame([declaration()])
        self.snap = pd.DataFrame([snapped()])
        self.stations = stations()
        self.tables = e.construire_exports(self.raw, self.snap, self.stations, self.stations)

    def validate(self, tables):
        e.valider_exports(tables, self.raw, self.stations, self.stations)

    def test_panel_requires_every_year_not_just_endpoints(self):
        raw = pd.DataFrame([declaration(site="full", year=y) for y in range(2020, 2025)]
                           + [declaration(site="gap", year=y) for y in (2020, 2021, 2023, 2024)])
        annual = e.construire_sites_annees(e.agreger_mois(raw))
        dim = e.construire_sites(raw, annual, self.snap.iloc[:0], 2020, 2024).set_index("num_site")
        self.assertTrue(dim.loc["full", "panel_2020_2024"])
        self.assertFalse(dim.loc["gap", "panel_2020_2024"])
        self.assertEqual(dim.loc["gap", "nb_annees_declarees"], 4)
        self.assertEqual(dim.loc["gap", "premiere_annee"], 2020)
        self.assertEqual(dim.loc["gap", "derniere_annee"], 2024)

    def test_panel_follows_configured_year_range(self):
        raw = pd.DataFrame([declaration(year=y) for y in range(2020, 2026)])
        annual = e.construire_sites_annees(e.agreger_mois(raw, 2020, 2025))
        dim = e.construire_sites(raw, annual, self.snap, 2020, 2025)
        self.assertTrue(dim.iloc[0].panel_2020_2025)

    def test_composites_preserved_and_uncharacterized_explicitly_skipped(self):
        self.assertEqual(self.tables.correspondance.station.tolist(), ["046709_08_01"])
        self.assertTrue(self.tables.correspondance.iloc[0].en_etiage)
        self.assertNotIn("000003", self.tables.correspondance.station.tolist())

    def test_no_top_twenty_limit_and_shared_sites_not_duplicated_in_facts(self):
        raw = pd.DataFrame([declaration(site=str(i)) for i in range(25)])
        snap = pd.DataFrame([snapped(site=str(i), flow=i % 2) for i in range(25)])
        st = stations(25)
        extra = st.iloc[[0]].copy()
        extra["station"] = "000004"
        st = pd.concat([st, extra], ignore_index=True)
        tables = e.construire_exports(raw, snap, st, st)
        self.assertEqual(len(tables.correspondance), 50)
        self.assertEqual(tables.correspondance.num_site.nunique(), 25)
        self.assertEqual(len(tables.sites_annees), 25)
        self.assertEqual(tables.correspondance.en_etiage.sum(), 24)

    def test_snap_deduplication_and_conflicting_snap_rejected(self):
        corr = e.construire_correspondance(self.stations, pd.concat([self.snap, self.snap]))
        self.assertEqual(len(corr), 1)
        conflicting = pd.DataFrame([snapped(), snapped(area=20)])
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            e.construire_correspondance(self.stations, conflicting)

    def test_basin_and_area_filter_both_apply_including_equal_area(self):
        snap = pd.DataFrame([snapped("001", area=100), snapped("002", area=101),
                             snapped("003", basin="outside")])
        corr = e.construire_correspondance(self.stations, snap)
        self.assertEqual(corr.num_site.tolist(), ["001"])

    def test_raw_surface_sites_can_exist_outside_published_correspondence(self):
        raw = pd.DataFrame([declaration(), declaration(site="outside", gross=0, consumed=0)])
        tables = e.construire_exports(raw, self.snap, self.stations, self.stations)
        self.assertEqual(set(tables.sites.num_site), {"001", "outside"})
        self.assertEqual(set(tables.correspondance.num_site), {"001"})
        self.assertTrue(pd.isna(tables.sites.set_index("num_site").loc["outside", "idtronc"]))

    def test_null_site_id_matches_legacy_groupby_scope_after_net_allocation(self):
        raw = pd.DataFrame([declaration(), declaration(site=None, gross=9000, consumed=7000)])
        before = raw.copy(deep=True)
        tables = e.construire_exports(raw, self.snap, self.stations, self.stations)
        self.assertEqual(tables.sites_annees.volume_brut_m3.sum(), 1)
        self.assertAlmostEqual(tables.sites_annees.conso_nette_annuel_m3s.sum(), .3 / 366 / 86400)
        pd.testing.assert_frame_equal(raw, before)

    def test_duplicate_keys_in_each_export_fail(self):
        for field in ("sites", "sites_annees", "correspondance"):
            values = {name: getattr(self.tables, name) for name in ("sites", "sites_annees", "correspondance")}
            values[field] = pd.concat([values[field], values[field]], ignore_index=True)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "duplicate key"):
                self.validate(e.EvolutionTables(**values))

    def test_coverage_mismatch_and_unknown_station_fail(self):
        wrong = self.stations.copy()
        wrong.loc[0, "n_sites_amont"] = 2
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            e.construire_exports(self.raw, self.snap, wrong, wrong)
        corr = self.tables.correspondance.copy()
        corr.loc[0, "station"] = "not_in_baseline"
        with self.assertRaisesRegex(ValueError, "absent stations"):
            self.validate(e.EvolutionTables(self.tables.sites, self.tables.sites_annees, corr))

    def test_annual_gross_net_and_default_source_total_mismatches_fail(self):
        for column in ("volume_brut_m3", "conso_nette_annuel_m3s", "conso_defaut100_annuel_m3s"):
            annual = self.tables.sites_annees.copy()
            annual.loc[0, column] += 1
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, "totals mismatch"):
                self.validate(e.EvolutionTables(self.tables.sites, annual, self.tables.correspondance))

    def test_empty_input_and_zero_site_station_coverage(self):
        st = stations(0)
        tables = e.construire_exports(self.raw.iloc[:0], self.snap.iloc[:0], st, st)
        self.assertTrue(tables.sites.empty)
        self.assertTrue(tables.sites_annees.empty)
        self.assertTrue(tables.correspondance.empty)

    def test_wrong_month_count_panel_and_unobserved_year_fail(self):
        annual = self.tables.sites_annees.copy()
        annual.loc[0, "mois_declares"] = 2
        with self.assertRaisesRegex(ValueError, "month count"):
            self.validate(e.EvolutionTables(self.tables.sites, annual, self.tables.correspondance))
        dim = self.tables.sites.copy()
        dim.loc[0, "panel_2020_2024"] = True
        with self.assertRaisesRegex(ValueError, "panel"):
            self.validate(e.EvolutionTables(dim, self.tables.sites_annees, self.tables.correspondance))
        annual = self.tables.sites_annees.copy()
        annual.loc[0, "annee"] = 2025
        with self.assertRaisesRegex(ValueError, "Annual keys"):
            self.validate(e.EvolutionTables(self.tables.sites, annual, self.tables.correspondance))

    def test_pure_module_does_not_import_geospatial_pipeline(self):
        self.assertNotIn("backend.build_details", sys.modules)
        self.assertNotIn("geopandas", sys.modules)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix=".evolution-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = self.root / "backend" / "data"
        web = self.root / "web" / "data"
        data.mkdir(parents=True)
        web.mkdir(parents=True)
        for path in (data / "resultats_pression_phase2.csv", data / "details_intervenants.csv",
                     data / "debits_etiage_cehq.csv", data / "coefficients_consommation.csv",
                     web / "etat_pression.json"):
            path.write_text("legacy\n", encoding="utf-8")
        self.baseline = self.root / "baseline.csv"
        stations().rename(columns={"superficie_km2": "superfi_km2"}).to_csv(self.baseline, index=False)
        self.etiages = self.root / "etiages.csv"
        self.etiages.write_text("baseline etiages\n", encoding="utf-8")
        self.excel = self.root / "source.xlsx"
        self.excel.write_bytes(b"read-only source fixture")
        raw = pd.DataFrame([declaration(year=y) for y in range(2020, 2025)])
        self.pipeline = SimpleNamespace(
            ROOT=data.parent, CHEMIN_ETIAGES=data / "debits_etiage_cehq.csv",
            CHEMIN_EXCEL=self.excel, CHEMIN_COEFFICIENTS=data / "coefficients_consommation.csv",
            ANNEE_MIN=2020, ANNEE_MAX=2024,
            charger_coefficients=Mock(return_value=({}, {})),
            charger_prelevements_5ans=Mock(return_value=raw),
            calculer_consommation=Mock(side_effect=lambda df, *_: df),
            calculer_profil_mensuel=Mock(return_value="retained legacy universe"),
            snap_sites_sur_reseau=Mock(return_value=pd.DataFrame([snapped()])),
            exporter_evolution=Mock(side_effect=e.construire_exports),
            main=Mock(side_effect=AssertionError("Legacy main must never run")),
            construire_stations=Mock(side_effect=AssertionError("Baseline stations are authoritative")),
            calculer_pression_et_details=Mock(side_effect=AssertionError("No pressure recalculation")),
        )
        self.original_etiages = self.pipeline.CHEMIN_ETIAGES
        self.pipeline.charger_reseau_et_stations = Mock(side_effect=self.load_network)

    def load_network(self):
        self.assertEqual(self.pipeline.CHEMIN_ETIAGES, self.etiages)
        return "atlas", "etiages"

    def run_generation(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = e.generer(self.baseline, self.etiages, pipeline=self.pipeline, **kwargs)
        return result, output.getvalue()

    def test_default_read_only_route_reuses_retained_universe_and_restores_override(self):
        before = e.empreintes_legacy(self.root, [self.baseline, self.etiages, self.excel])
        _, output = self.run_generation()
        e.verifier_empreintes(before)
        self.assertIn("uncharacterized skipped: 1", output)
        self.assertIn("not evidence of physical absence", output)
        self.assertEqual(self.pipeline.CHEMIN_ETIAGES, self.original_etiages)
        self.pipeline.snap_sites_sur_reseau.assert_called_once_with("retained legacy universe", "atlas")
        self.pipeline.main.assert_not_called()
        self.pipeline.construire_stations.assert_not_called()
        self.pipeline.calculer_pression_et_details.assert_not_called()
        for name in e.EXPORT_NAMES:
            self.assertFalse((self.root / "backend" / "data" / name).exists())

    def test_write_only_three_exports_and_identical_pages_copies(self):
        before = e.empreintes_legacy(self.root, [self.baseline, self.etiages, self.excel])
        self.run_generation(write=True, expected_station_sha256=e.sha256(self.baseline))
        e.verifier_empreintes(before)
        for name in e.EXPORT_NAMES:
            backend = self.root / "backend" / "data" / name
            web = self.root / "web" / "data" / name
            self.assertEqual(backend.read_bytes(), web.read_bytes())
        corr = pd.read_csv(self.root / "web" / "data" / e.EXPORT_NAMES[2], dtype={"station": str})
        self.assertEqual(corr.station.tolist(), ["046709_08_01"])

    def test_write_requires_approved_hash_before_loading_pipeline(self):
        with self.assertRaisesRegex(ValueError, "requires"):
            self.run_generation(write=True)
        with self.assertRaisesRegex(ValueError, "hash"):
            self.run_generation(write=True, expected_station_sha256="wrong")
        self.pipeline.charger_prelevements_5ans.assert_not_called()

    def test_coverage_failure_writes_nothing_and_restores_override(self):
        self.pipeline.snap_sites_sur_reseau.return_value = pd.DataFrame([snapped(basin="elsewhere")])
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            self.run_generation(write=True, expected_station_sha256=e.sha256(self.baseline))
        self.assertEqual(self.pipeline.CHEMIN_ETIAGES, self.original_etiages)
        self.assertFalse((self.root / "backend" / "data" / e.EXPORT_NAMES[0]).exists())

    def test_missing_annual_source_sheet_stops(self):
        self.pipeline.charger_prelevements_5ans.return_value = pd.DataFrame([declaration()])
        with self.assertRaisesRegex(ValueError, "source sheets"):
            self.run_generation()
        self.assertEqual(self.pipeline.CHEMIN_ETIAGES, self.original_etiages)

    def test_hash_guard_blocks_exports_after_legacy_changes(self):
        before = e.empreintes_legacy(self.root)
        path = self.root / "backend" / "data" / "details_intervenants.csv"
        path.write_text("modified", encoding="utf-8")
        empty = pd.DataFrame()
        with self.assertRaisesRegex(ValueError, "hashes changed"):
            e.ecrire_exports(e.EvolutionTables(empty, empty, empty), self.root / "backend" / "data",
                             self.root / "web" / "data", before)
        self.assertFalse((self.root / "backend" / "data" / e.EXPORT_NAMES[0]).exists())


if __name__ == "__main__":
    unittest.main()
