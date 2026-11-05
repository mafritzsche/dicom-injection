"""
load_manifest.py
----------------
Lädt die im Manifest gelisteten Serien nach Orthanc. Standard: die
gemittelten 5-mm-Serien (orthanc_80_5mm_manifest.json, Dateien in
cq-500-5mm/, erzeugt mit build_5mm_series.py). Orthanc enthält nur diese;
die dünnen Quellserien (orthanc_80_manifest.json, cq-500-dicoms/) werden
nicht hochgeladen.

Pro Manifest-Eintrag wird im Patientenordner die Serie mit passender
SeriesInstanceUID gesucht und genau die gelisteten SOPInstanceUIDs
hochgeladen. Bereits vorhandene Instanzen werden übersprungen, ein
erneuter Lauf ist daher unbedenklich.

Aufruf:
    python load_manifest.py
    python load_manifest.py --manifest ../orthanc_80_5mm_manifest.json --dicom-dir ../cq-500-5mm
"""

import argparse
import json
import sys
from pathlib import Path

import pydicom
import requests
from tqdm import tqdm

from config import REPO_ROOT, ORTHANC_URL, ORTHANC_USER, ORTHANC_PASS


def _auth():
    return (ORTHANC_USER, ORTHANC_PASS) if ORTHANC_USER else None


def _header(path: Path, tag: str):
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=[tag])
        return str(ds.get(tag, ""))
    except Exception:
        return None


def _find_series_dir(patient_dir: Path, series_uid: str):
    """Serienordner, dessen erste lesbare Datei die gesuchte SeriesInstanceUID trägt."""
    for series_dir in sorted({p.parent for p in patient_dir.rglob("*") if p.is_file()}):
        for f in sorted(series_dir.iterdir()):
            if f.is_file():
                uid = _header(f, "SeriesInstanceUID")
                if uid is not None:
                    if uid == series_uid:
                        return series_dir
                    break
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=REPO_ROOT / "orthanc_80_5mm_manifest.json", type=Path)
    ap.add_argument("--dicom-dir", default=REPO_ROOT / "cq-500-5mm", type=Path)
    args = ap.parse_args()

    requests.get(f"{ORTHANC_URL}/system", auth=_auth(), timeout=10).raise_for_status()

    manifest = json.loads(args.manifest.read_text())
    problems = []

    for entry in tqdm(manifest, desc="Serien"):
        pid = entry["PatientID"]
        wanted = set(entry["SOPInstanceUIDs"])
        series_dir = _find_series_dir(args.dicom_dir / pid, entry["SeriesInstanceUID"])
        if series_dir is None:
            problems.append(f"{pid}: Serie {entry['SeriesInstanceUID']} nicht gefunden")
            continue

        uploaded = set()
        for f in sorted(series_dir.iterdir()):
            sop = _header(f, "SOPInstanceUID") if f.is_file() else None
            if sop not in wanted or sop in uploaded:
                continue
            resp = requests.post(
                f"{ORTHANC_URL}/instances", data=f.read_bytes(), auth=_auth(), timeout=60
            )
            resp.raise_for_status()
            uploaded.add(sop)

        missing = wanted - uploaded
        if missing:
            problems.append(f"{pid}: {len(missing)} von {len(wanted)} Instanzen fehlen")

    stats = requests.get(f"{ORTHANC_URL}/statistics", auth=_auth(), timeout=10).json()
    print(f"Orthanc: {stats['CountStudies']} Studien, {stats['CountInstances']} Instanzen")
    for p in problems:
        print("  !", p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
