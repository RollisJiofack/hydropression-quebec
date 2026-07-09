"""Garde-fou Python pour HydroPression.

Le workflow lance `python generate_state.py` depuis le dossier backend. Python charge
automatiquement un fichier `sitecustomize.py` présent sur le chemin d'import.

Objectif : éviter les commits horaires trompeurs lorsque la source live CEHQ/MSP
reste bloquée et que le JSON généré ne change que par ses horodatages de panne.
Si les données live reviennent, ou si le contenu métier change réellement, le
nouveau JSON est conservé.
"""

from __future__ import annotations

import atexit
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT.parent / "web" / "data" / "etat_pression.json"
DISABLED = os.environ.get("HP_DISABLE_STALE_NOOP") == "1"

_previous_bytes: bytes | None = None

if not DISABLED:
    try:
        _previous_bytes = OUTPUT_PATH.read_bytes()
    except OSError:
        _previous_bytes = None


def _stable_payload(data: dict) -> dict:
    """Retourne la partie utile du JSON, sans les champs purement volatils."""
    out = copy.deepcopy(data)
    out.pop("generated_at", None)
    out.pop("fetch_status", None)
    return out


def _restore_previous_if_stale_only() -> None:
    if DISABLED or _previous_bytes is None or not OUTPUT_PATH.exists():
        return

    try:
        previous = json.loads(_previous_bytes.decode("utf-8"))
        current = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return

    stale_again = (
        current.get("data_stale") is True
        and previous.get("data_stale") is True
        and int(current.get("n_stations_debit_live") or 0) == 0
        and int(previous.get("n_stations_debit_live") or 0) == 0
    )

    if stale_again and _stable_payload(current) == _stable_payload(previous):
        try:
            OUTPUT_PATH.write_bytes(_previous_bytes)
            print(
                "API live toujours indisponible : ancien JSON conservé, "
                "aucun commit horaire trompeur."
            )
        except OSError as exc:
            print(f"Impossible de restaurer l'ancien JSON : {exc}")


atexit.register(_restore_previous_if_stale_only)
