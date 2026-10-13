"""Read transitory mapping warnings without changing pressure calculations."""

import csv
from datetime import date
from pathlib import Path

FLAGS_PATH = Path(__file__).resolve().parent / "data/stations_signalees.csv"


def load_station_flags(path=FLAGS_PATH):
    flags = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["station", "motif", "date"]:
            raise ValueError("Expected station,motif,date columns")
        for row in reader:
            code = row["station"].strip()
            reason = row["motif"].strip()
            if not code or not reason or code in flags or None in row:
                raise ValueError("Missing or duplicate station warning")
            date.fromisoformat(row["date"])
            flags[code] = {"signalement_motif": reason, "signalement_date": row["date"]}
    return flags
