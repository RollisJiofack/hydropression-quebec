import ast
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from backend import evolution as e


def profile_function():
    # Exercise the production step 2 without importing the GIS-only pipeline.
    path = Path(__file__).resolve().parents[1] / "build_details.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "calculer_profil_mensuel")
    namespace = {"pd": pd, "evolution": e, "ANNEE_MIN": 2020,
                 "ANNEE_MAX": 2024, "MOIS_ETIAGE": (7, 8)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[node.name]


def declaration(volume, days=31, year=2020, month=7, site=1):
    return {e.SITE: site, e.YEAR: year, e.MONTH: month, e.DAYS: days,
            e.GROSS: volume, "vol_consomme_L": volume,
            "source_consommation": "1_donnee_declaree", "Longitude (site)": -72,
            "Latitude (site)": 47, "Nom de l'intervenant": "Test",
            "Municipalit\u00e9": "Test", "Description du code SCIAN": "Test",
            "Code SCIAN par site par mois": 1111,
            "debit_consomme_jour_m3s": volume / days / 86400000}


class ProductionProfileTests(unittest.TestCase):
    def run_profile(self, rows):
        with contextlib.redirect_stdout(io.StringIO()):
            return profile_function()(pd.DataFrame(rows)).iloc[0]

    def test_two_scian_rows_sum_before_division(self):
        row = self.run_profile([declaration(1000), declaration(3000)])
        self.assertAlmostEqual(row["debit_mois_07_m3s"], 4000 / 31 / 86400000)

    def test_different_days_use_maximum(self):
        row = self.run_profile([declaration(1000, days=10), declaration(3000, days=20)])
        self.assertAlmostEqual(row["debit_mois_07_m3s"], 4000 / 20 / 86400000)

    def test_average_years_not_individual_rows(self):
        row = self.run_profile([declaration(1000), declaration(3000), declaration(2000, year=2024)])
        self.assertAlmostEqual(row["debit_mois_07_m3s"], 3000 / 31 / 86400000)
        self.assertAlmostEqual(row["volume_annuel_moyen_Mm3"], 3000 / 1e9)

    def test_summer_max_after_mean_and_missing_month_zero(self):
        row = self.run_profile([declaration(1000), declaration(9000, year=2024),
                                declaration(6000, month=8), declaration(6000, year=2024, month=8)])
        self.assertAlmostEqual(row["debit_etiage_m3s"], 6000 / 31 / 86400000)
        self.assertEqual(row["debit_mois_01_m3s"], 0)

    def test_reuses_single_aggregation_rule(self):
        with patch.object(e, "agreger_mois", wraps=e.agreger_mois) as aggregation:
            row = self.run_profile([declaration(1000, site="001")])
        aggregation.assert_called_once()
        self.assertEqual(row[e.SITE], "001")
