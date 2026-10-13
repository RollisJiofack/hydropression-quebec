"""Offline structural/CSV tests, deliberately NOT semantic DAX engine tests."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

import generate as g


class DeploymentTests(unittest.TestCase):
    def test_exact_additive_scope(self):
        report = g.validate_structure(g.deployment_script())
        self.assertEqual(report["tables"], list(g.TABLES))
        self.assertEqual(report["relationships"], 3)
        self.assertEqual(report["measures"], 13)

    def test_authoritative_generated_files_match_sources(self):
        root = g.ROOT / "generated"
        self.assertEqual((root / "deploy.tmdl").read_text(encoding="utf-8"), g.deployment_script())
        for table in g.TABLES:
            self.assertEqual((root / f"{table}.tmdl").read_text(encoding="utf-8"), g.tmdl_table(table))
            self.assertEqual((root / f"{table}.pq").read_text(encoding="utf-8"), g.m_source(table) + "\n")

    def test_manifest_hash(self):
        import hashlib
        root = g.ROOT / "generated"
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual(manifest["deploy_sha256"], hashlib.sha256((root / "deploy.tmdl").read_bytes()).hexdigest())

    def test_deterministic_relationship_ids_and_direction(self):
        script = g.tmdl_relationships()
        self.assertEqual(script, g.tmdl_relationships())
        self.assertEqual(script.count("crossFilteringBehavior: oneDirection"), 3)
        self.assertNotIn("Grandeur", script)
        self.assertNotIn("toColumn: Correspondance", script)

    def test_existing_tables_rejected_as_targets(self):
        with self.assertRaises(ValueError):
            g.validate_structure(g.deployment_script() + "\ttable DimStation\n")
        with self.assertRaises(ValueError):
            g.validate_structure(g.deployment_script() + "\tref table FactPressionSnapshot\n")

    def test_static_sources_typed_nullable_and_no_cache(self):
        for table, (filename, _) in g.TABLES.items():
            if filename:
                source = g.m_source(table)
                self.assertIn(f'Web.Contents("{g.BASE_URL}"', source)
                self.assertIn(f'RelativePath = "data/{filename}"', source)
                self.assertIn('#"Cache-Control" = "no-cache"', source)
                self.assertIn('Encoding = 65001', source)
                self.assertIn('MissingField.Error', source)
                self.assertIn('"", null', source)
                self.assertNotIn('"", 0', source)
                self.assertIn('"en-US"', source)

    def test_years_from_facts_and_grandeur_four_rows(self):
        self.assertIn("Source = FactSiteAnnee", g.m_source("DimAnnee"))
        self.assertIn("Table.Distinct", g.m_source("DimAnnee"))
        for index in range(1, 5):
            self.assertIn("{" + str(index) + ",", g.m_source("Grandeur"))
        self.assertNotIn("{5,", g.m_source("Grandeur"))
        self.assertIn("sortByColumn: index", g.tmdl_table("Grandeur"))

    def test_year_guards_and_distinct_site_transfer(self):
        for name in ("Brut annuel (m3/s)", "Brut juillet-aout (m3/s)", "Nette annuelle (m3/s)", "Nette juillet-aout (m3/s)", "Valeur tous les sites", "Sites declarants"):
            self.assertIn("HASONEVALUE ( DimAnnee[annee] )", g.MEASURES[name]["expression"])
        for name in ("Brut annuel (m3/s)", "Brut juillet-aout (m3/s)", "Nette annuelle (m3/s)", "Nette juillet-aout (m3/s)"):
            expression = g.MEASURES[name]["expression"]
            self.assertIn("VALUES ( CorrespondanceSiteStation[num_site] )", expression)
            self.assertIn("KEEPFILTERS ( TREATAS", expression)
            self.assertNotIn("COALESCE", expression)

    def test_kpi_removes_all_year_filters_at_both_endpoints(self):
        expression = g.MEASURES["Evolution du panel (%)"]["expression"]
        for endpoint in ("a0", "a1"):
            self.assertIn(f"REMOVEFILTERS ( DimAnnee ), TREATAS ( {{ {endpoint} }}, DimAnnee[annee] )", expression)
        self.assertIn("a0 < a1", expression)
        self.assertNotIn("ALL ( FactSiteAnnee", expression)

    def test_ratio_restricted_and_blank_preserved(self):
        self.assertIn("g = 3 && HASONEVALUE", g.MEASURES["Part estimee a 100 %"]["expression"])
        self.assertNotIn("COALESCE", g.MEASURES["Valeur panel constant"]["expression"])
        self.assertNotIn("Effet de composition", g.MEASURES)
        self.assertIn("NOT ISBLANK ( p )", g.MEASURES["Contribution hors panel (m3/s)"]["expression"])

    def test_subtitle_required_warnings(self):
        expression = g.MEASURES["Sous-titre evolution"]["expression"]
        for phrase in ("365/366", "sans imputation", "annee partielle", "non necessairement simultanes", "pas un total quebecois", "Aucun site", "panel absent", "methode d'estimation"):
            self.assertIn(phrase, expression)
        self.assertIn("REMOVEFILTERS ( DimAnnee )", expression)

    def test_formats_and_measure_home(self):
        text = g.tmdl_table("FactSiteAnnee")
        for name, spec in g.MEASURES.items():
            self.assertIn("measure '" + name + "'", text)
            if name.startswith("Sites"):
                self.assertEqual(spec["format"], "0")
            elif name in ("Evolution du panel (%)", "Part estimee a 100 %"):
                self.assertEqual(spec["format"], "0.0%")
            elif name != "Sous-titre evolution":
                self.assertEqual(spec["format"], "0.000")


class CsvContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-csv-", dir=g.ROOT)
        self.root = Path(self.temp.name)
        self.rows = {"FactSiteAnnee": [], "DimSite": [], "CorrespondanceSiteStation": []}
        for site, years in (("0001", range(2020, 2025)), ("0002", (2021, 2024))):
            for year in years:
                seconds = (366 if year in (2020, 2024) else 365) * 86400
                self.rows["FactSiteAnnee"].append(dict(num_site=site, annee=year, mois_declares=6, volume_brut_m3=seconds * 0.001, debit_brut_annuel_m3s=0.001, debit_brut_juilaout_m3s="", conso_nette_annuel_m3s=0.0005, conso_nette_juilaout_m3s="", conso_defaut100_annuel_m3s=0.0001))
            self.rows["DimSite"].append(dict(num_site=site, nom_intervenant="Example", municipalite="", secteur_scian="", code_scian="0011", premiere_annee=min(years), derniere_annee=max(years), nb_annees_declarees=len(years), panel_2020_2024=len(years) == 5, idtronc="001", bv_prim="01"))
        self.rows["CorrespondanceSiteStation"] = [dict(station="046709_08_01", num_site="0001", en_etiage=True), dict(station="020802", num_site="0001", en_etiage=False), dict(station="020802", num_site="0002", en_etiage=False)]

    def tearDown(self):
        self.temp.cleanup()

    def write_fixture(self):
        for table, rows in self.rows.items():
            filename, columns = g.TABLES[table]
            with (self.root / filename).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=[n for n, _, _ in columns])
                writer.writeheader()
                writer.writerows(rows)

    def validate(self):
        self.write_fixture()
        return g.validate_csv_contract(self.root)

    def test_valid_leap_calendar_nullable_summer_shared_site_composite_code(self):
        report = self.validate()
        self.assertEqual(report["years"], [2020, 2021, 2022, 2023, 2024])
        self.assertEqual(report["rows"]["FactSiteAnnee"], 7)

    def test_duplicates_rejected_in_each_grain(self):
        for table in self.rows:
            with self.subTest(table=table):
                self.rows[table].append(self.rows[table][0].copy())
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    self.validate()
                self.rows[table].pop()

    def test_calendar_365_in_leap_year_rejected(self):
        self.rows["FactSiteAnnee"][0]["volume_brut_m3"] = 365 * 86400 * 0.001
        with self.assertRaisesRegex(ValueError, "calendar denominator"):
            self.validate()

    def test_panel_attributes_rejected(self):
        self.rows["DimSite"][1]["panel_2020_2024"] = True
        with self.assertRaisesRegex(ValueError, "Panel/year"):
            self.validate()

    def test_unresolved_site_rejected(self):
        self.rows["CorrespondanceSiteStation"][0]["num_site"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Invalid correspondence"):
            self.validate()

    def test_zero_months_and_missing_volume_rejected(self):
        self.rows["FactSiteAnnee"][0]["mois_declares"] = 0
        with self.assertRaisesRegex(ValueError, "mois_declares"):
            self.validate()
        self.rows["FactSiteAnnee"][0]["mois_declares"] = 6
        self.rows["FactSiteAnnee"][0]["volume_brut_m3"] = ""
        with self.assertRaisesRegex(ValueError, "Missing annual"):
            self.validate()

    def test_numeric_boolean_serialization(self):
        for row in self.rows["DimSite"]:
            row["panel_2020_2024"] = int(row["panel_2020_2024"])
        for row in self.rows["CorrespondanceSiteStation"]:
            row["en_etiage"] = int(row["en_etiage"])
        self.validate()

    def test_nonfinite_and_negative_values_rejected(self):
        for value in (float("nan"), float("inf"), -1):
            with self.subTest(value=value):
                self.rows["FactSiteAnnee"][0]["volume_brut_m3"] = value
                with self.assertRaises(ValueError):
                    self.validate()

    def test_missing_header_rejected(self):
        self.write_fixture()
        (self.root / "sites.csv").write_text("num_site\n0001\n")
        with self.assertRaisesRegex(ValueError, "missing columns"):
            g.validate_csv_contract(self.root)


if __name__ == "__main__":
    unittest.main()
