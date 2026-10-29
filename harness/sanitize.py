"""
sanitize.py
-----------
Header-Bereinigung für den Abwehrarm 1.2 ("Header-Bereinigung").

Grundlage ist die Allowlist aus `header_tabelle_alle.xlsx` (Sheets "Allowlist"
und "Vokabular"), die als `header_allowlist.json` neben diesem Modul liegt:

  - SeriesDescription: nur die 60 zugelassenen Protokollwerte
  - StudyDescription:  Vokabular ist leer -> jeder nicht-leere Wert ist anomal
  - ImageComments:     im CQ500-Datensatz durchgehend leer -> jeder Wert anomal

Bereinigt wird nur, was das Modell zu sehen bekommt. Die DICOM-Kopie in Orthanc
bleibt unverändert - so lässt sich derselbe Lauf mit und ohne Bereinigung gegen
dieselben Daten stellen.
"""

import json
from pathlib import Path

_ALLOWLIST_PATH = Path(__file__).parent / "header_allowlist.json"

with open(_ALLOWLIST_PATH, encoding="utf-8") as f:
    _ALLOWLIST = json.load(f)

SERIES_DESCRIPTION_ALLOWLIST = _ALLOWLIST["series_description_allowlist"]
STUDY_DESCRIPTION_VOCABULARY = _ALLOWLIST["study_description_vocabulary"]

# Vergleich ohne Rücksicht auf Groß-/Kleinschreibung: die Liste enthält
# Varianten wie "PLAIN THIN" und "Plain THIN".
_SERIES_LOWER = {w.lower() for w in SERIES_DESCRIPTION_ALLOWLIST}
_STUDY_LOWER = {w.lower() for w in STUDY_DESCRIPTION_VOCABULARY}


def sanitize_series_description(value: str) -> str:
    """Zugelassener Protokollwert bleibt, alles andere wird entfernt."""
    value = (value or "").strip()
    if not value:
        return ""
    return value if value.lower() in _SERIES_LOWER else ""


def sanitize_study_description(value: str) -> str:
    """Das Vokabular ist leer - jeder Wert ist anomal und wird entfernt."""
    value = (value or "").strip()
    if not value:
        return ""
    return value if value.lower() in _STUDY_LOWER else ""


def sanitize_image_comments(value: str) -> str:
    """Im CQ500-Datensatz durchgehend leer - jeder Wert ist anomal."""
    return ""


def sanitize_study_header(header: dict) -> dict:
    """
    Bereinigt einen Header, wie ihn read_header an das Modell gibt.
    Gibt eine Kopie zurück; das Original bleibt unberührt.
    """
    bereinigt = dict(header)
    bereinigt["StudyDescription"] = sanitize_study_description(
        header.get("StudyDescription", "")
    )
    bereinigt["ImageComments"] = sanitize_image_comments(
        header.get("ImageComments", "")
    )
    bereinigt["series"] = [
        {
            **serie,
            "SeriesDescription": sanitize_series_description(
                serie.get("SeriesDescription", "")
            ),
        }
        for serie in header.get("series", [])
    ]
    return bereinigt
