import contextlib
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from backend import generate_state as state


class FreshnessTests(unittest.TestCase):
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)

    def build(self, live=None, previous=None, **columns):
        row = {
            "station": "040129", "station_norm": "40129", "nom": "Test",
            "plan_deau": "Test", "bv_prim": "Test", "superfi_km2": 1,
            "n_sites_amont": 1, "n_sites_etiage": 1, "lat": 47, "lon": -72,
            "prelevements_caracterises": 1, "debit_preleve_mois_10_m3s": 1,
            "pression_etiage_pct": 60, "debit_preleve_etiage_m3s": 1,
            "q27_ete_m3s": 0.667,
        }
        row.update(columns)
        stamp = (self.now - timedelta(minutes=15)).isoformat()
        metadata = {"ok": True, "latest_measure_utc": stamp, "median_measure_utc": stamp}
        with contextlib.redirect_stdout(io.StringIO()):
            return state.compute_state(pd.DataFrame([row]), live or {}, {}, 10,
                                       previous, metadata, now=self.now)

    def live(self, flow=3, age=0.25):
        return {"40129": {"debit_obs_m3s": flow,
                           "date_mesure": (self.now - timedelta(hours=age)).isoformat()}}

    def test_recent_pressure_formula_unchanged(self):
        data = self.build(self.live())
        self.assertEqual(data["stations"][0]["pression_observe_pct"], 25)
        self.assertEqual(data["n_stations_debit_recent"], 1)

    def test_stale_live_excluded_but_retained(self):
        data = self.build(self.live(age=7))
        station = data["stations"][0]
        self.assertIsNone(station["debit_obs_m3s"])
        self.assertIsNone(station["pression_observe_pct"])
        self.assertEqual(station["categorie_observe"], "inconnu")
        self.assertEqual(station["debit_obs_dernier_connu_m3s"], 3)
        self.assertEqual(data["n_stations_debit_perime"], 1)
        self.assertTrue(data["data_stale"])

    def test_previous_old_value_survives_repeated_runs(self):
        first = self.build(self.live(age=1000))["stations"][0]
        second = self.build(previous={"40129": first})["stations"][0]
        self.assertEqual(second["debit_obs_dernier_connu_m3s"], 3)
        self.assertEqual(second["date_mesure"], first["date_mesure"])
        self.assertIsNone(second["pression_observe_pct"])

    def test_new_level_does_not_refresh_old_flow_timestamp(self):
        previous = {"40129": {"debit_obs_m3s": 3,
                                "date_mesure": (self.now - timedelta(days=3)).isoformat()}}
        live = self.live(flow=None)
        live["40129"]["niveau_m"] = 2
        station = self.build(live, previous)["stations"][0]
        self.assertEqual(station["statut_debit_observe"], "perime")
        self.assertEqual(station["date_mesure"], previous["40129"]["date_mesure"])

    def test_undated_csv_cannot_borrow_previous_date(self):
        previous = {"40129": {"debit_obs_m3s": 3, "date_mesure": self.now.isoformat()}}
        station = self.build(previous=previous, debit_obs_m3s=4)["stations"][0]
        self.assertEqual(station["statut_debit_observe"], "non_date")
        self.assertIsNone(station["pression_observe_pct"])

    def test_threshold_boundary(self):
        self.assertEqual(self.build(self.live(age=6))["stations"][0]["statut_debit_observe"], "recent")
        self.assertEqual(self.build(self.live(age=6.001))["stations"][0]["statut_debit_observe"], "perime")

    def test_future_measurement_excluded(self):
        station = self.build(self.live(age=-1))["stations"][0]
        self.assertEqual(station["statut_debit_observe"], "date_future")
        self.assertIsNone(station["pression_observe_pct"])

    def test_zero_flow_is_valid_not_missing(self):
        station = self.build(self.live(flow=0))["stations"][0]
        self.assertEqual(station["debit_obs_m3s"], 0)
        self.assertEqual(station["pression_observe_pct"], 100)

    def test_absent_flow_and_missing_consumption_not_zero(self):
        self.assertIsNone(self.build()["stations"][0]["pression_observe_pct"])
        station = self.build(self.live(), debit_preleve_mois_10_m3s=float("nan"))["stations"][0]
        self.assertIsNone(station["debit_preleve_m3s"])
        self.assertIsNone(station["pression_observe_pct"])

    def test_uncharacterised_pressure_remains_unknown(self):
        station = self.build(self.live(), prelevements_caracterises=0)["stations"][0]
        self.assertIsNone(station["pression_observe_pct"])

    def test_lowflow_assessment_unchanged_when_current_unavailable(self):
        data = self.build(self.live(age=1000))
        self.assertEqual(data["stations"][0]["pression_etiage_pct"], 60)
        self.assertEqual(data["n_critiques_etiage"], 1)

    def test_non_finite_and_negative_flows_excluded(self):
        for flow in (float("inf"), float("nan"), -1):
            with self.subTest(flow=flow):
                self.assertIsNone(self.build(self.live(flow=flow))["stations"][0]["pression_observe_pct"])

    def test_collection_freshness_uses_flow_timestamps_not_levels(self):
        old = self.now - timedelta(days=2)
        records = [
            ({"debit_obs_m3s": 3, "dt_utc": old}, "ok"),
            ({"debit_obs_m3s": None, "niveau_m": 1, "dt_utc": self.now}, "ok"),
        ]
        with patch.object(state.pd, "read_csv", return_value=pd.DataFrame({"station": [10101, 10102]})), \
                patch.object(state, "_fetch_station_cehq", side_effect=records), \
                patch.object(state.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            _, metadata = state.fetch_stations()
        self.assertEqual(metadata["latest_measure_utc"], old.isoformat())
        self.assertEqual(metadata["median_measure_utc"], old.isoformat())


if __name__ == "__main__":
    unittest.main()
