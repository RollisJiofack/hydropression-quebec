import contextlib
import io
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from backend import generate_state, station_flags


class StationFlagsTests(unittest.TestCase):
    def test_exact_three_warnings_and_no_second_public_source(self):
        flags = station_flags.load_station_flags()
        self.assertEqual(set(flags), {"050119", "050135", "050144_16"})
        for value in flags.values():
            self.assertEqual(value["signalement_motif"], "Rattachement contest\u00e9 : pression \u00e0 confirmer")
            self.assertEqual(value["signalement_date"], "2026-10-13")
        public = station_flags.FLAGS_PATH.parents[2] / "web/data/stations_signalees.csv"
        self.assertFalse(public.exists())

    def test_invalid_csv_fails_without_silent_omission(self):
        for text in ("station,motif,date\na,test,2026-02-30\n",
                     "station,motif,date\na,test,2026-10-13\na,test,2026-10-13\n",
                     "code,motif,date\na,test,2026-10-13\n",
                     "station,motif,date\na,,2026-10-13\n"):
            with patch.object(Path, "open", return_value=io.StringIO(text)):
                with self.assertRaises(ValueError):
                    station_flags.load_station_flags()

    def test_json_mentions_only_flagged_and_changes_no_pressure_or_count(self):
        now = datetime(2026, 10, 13, 17, tzinfo=timezone.utc)
        codes = ["050119", "050135", "050144_16", "040409"]
        rows = [{"station": c, "station_norm": c, "nom": "Test", "plan_deau": "Test",
                 "bv_prim": "Test", "superfi_km2": 1, "n_sites_amont": 1,
                 "n_sites_etiage": 1, "pression_etiage_pct": 20, "q27_ete_m3s": 4,
                 "debit_preleve_etiage_m3s": 1, "debit_preleve_mois_10_m3s": 1,
                 "prelevements_caracterises": 1} for c in codes]
        live = {c: {"debit_obs_m3s": 3, "date_mesure": now.isoformat()} for c in codes}
        with contextlib.redirect_stdout(io.StringIO()):
            base = generate_state.compute_state(pd.DataFrame(rows), live, {}, 10, now=now, station_flags={})
            warned = generate_state.compute_state(pd.DataFrame(rows), live, {}, 10, now=now)
        flagged = {s["code"] for s in warned["stations"] if s["signalement_motif"]}
        self.assertEqual(flagged, set(codes[:3]))
        for data in (base, warned):
            for station in data["stations"]:
                station.pop("signalement_motif")
                station.pop("signalement_date")
        self.assertEqual(base, warned)
