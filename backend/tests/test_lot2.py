import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend import generate_state as state
from backend.tests import test_freshness


class GenerationMetadataTests(unittest.TestCase):
    def test_six_hour_boundary_in_minutes(self):
        at = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        for minutes, expected in [(359, "recent"), (360, "recent"), (361, "perime")]:
            self.assertEqual(state.debit_freshness(3, (at - timedelta(minutes=minutes)).isoformat(), at)[0], expected)

    def test_future_measure_is_not_recent(self):
        at = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        self.assertEqual(state.debit_freshness(3, (at + timedelta(minutes=1)).isoformat(), at)[0], "date_future")

    def test_toronto_offset_is_generated_in_python(self):
        helper = test_freshness.FreshnessTests()
        data = helper.build(helper.live())
        self.assertEqual(data["generated_at_local"], "2026-10-09T08:00:00-04:00")

    def test_gap_uses_absolute_instants(self):
        at = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        self.assertEqual(state.generation_gap_hours(at, "2026-10-13T21:30:00-04:00"), 2.5)
        self.assertIsNone(state.generation_gap_hours(at, None))
        self.assertIsNone(state.generation_gap_hours(at, "invalid"))
        self.assertIsNone(state.generation_gap_hours(at, "2026-10-15T00:00:00Z"))

    def test_warning_only_after_two_hours_and_without_failure(self):
        for gap, expected in [(None, False), (2, False), (2.0001, True)]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertIsNone(state.warn_generation_gap({"ecart_generation_precedente_h": gap}))
            self.assertEqual("::warning::" in output.getvalue(), expected)

    def test_previous_generation_read_before_overwrite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            target = Path(folder) / "etat.json"
            target.write_text('{"generated_at":"2026-10-13T23:37:00Z"}', encoding="utf-8")
            with patch.object(state, "OUTPUT_PATH", target):
                previous = state.load_previous_generation()
                target.write_text("{}", encoding="utf-8")
            self.assertEqual(previous, "2026-10-13T23:37:00Z")

    def test_complete_intervenants_preserve_summer_total_and_inactive_count(self):
        import pandas as pd
        row = {"station": "040409", "station_norm": "40409", "nom": "Test", "plan_deau": "Test",
               "bv_prim": "Test", "superfi_km2": 1, "n_sites_amont": 4, "n_sites_etiage": 4,
               "lat": 47, "lon": -72, "prelevements_caracterises": 1,
               "debit_preleve_mois_10_m3s": 3, "pression_etiage_pct": 28,
               "debit_preleve_etiage_m3s": 20, "q27_ete_m3s": 50}
        details = {"40409": [
            {"num_site": "A", "debit_mois_10_m3s": 1, "debit_etiage_m3s": 3},
            {"num_site": "B", "debit_mois_10_m3s": 0, "debit_etiage_m3s": 7},
            {"num_site": "C", "debit_mois_10_m3s": 2, "debit_etiage_m3s": 2},
            {"num_site": "D", "debit_mois_10_m3s": 0, "debit_etiage_m3s": 8},
        ]}
        with contextlib.redirect_stdout(io.StringIO()):
            result = state.compute_state(pd.DataFrame([row]), {}, details, 10, station_flags={})["stations"][0]
        self.assertEqual([i["num_site"] for i in result["intervenants"]], ["C", "A", "D", "B"])
        self.assertEqual([i["actif_mois_courant"] for i in result["intervenants"]], [True, True, False, False])
        self.assertEqual(result["n_sites_inactifs_mois"], 2)
        self.assertEqual(sum(i["debit_etiage_m3s"] for i in result["intervenants"]), 20)


if __name__ == "__main__":
    unittest.main()
