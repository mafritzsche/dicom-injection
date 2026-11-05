"""
build_5mm_series.py
-------------------
Erzeugt aus den dünnen Serien (orthanc_80_manifest.json, Dateien in
cq-500-dicoms/) je eine gemittelte 5-mm-Serie, nur über den Gehirnbereich,
und schreibt sie als DICOM nach cq-500-5mm/ sowie das Manifest
orthanc_80_5mm_manifest.json. Orthanc wird nicht angefasst; hochgeladen wird
mit load_manifest.py.

Ablauf pro Fall (siehe docs/design_decisions.md):
  1. Dünne Schichten nach Position (z) sortieren, in HU umrechnen.
  2. Gehirnbereich bestimmen: Fläche von Gewebe (0-100 HU) innerhalb des
     Schädelknochens pro Schicht; oben bis nur noch Kalotte, unten mit
     hinterer Schädelgrube, Messlücken an der Schädelbasis überbrückt.
  3. Lückenlose 5-mm-Blöcke ohne Überlappung bilden und mitteln.
  4. Pro Block ein DICOM-Bild: Kopf aus der mittleren dünnen Schicht, neue
     reproduzierbare UIDs, abgeleiteter Serienname.

Aufruf:
    python build_5mm_series.py
    python build_5mm_series.py --only CQ500-CT-102 CQ500-CT-112
"""

import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.uid import ExplicitVRLittleEndian
from tqdm import tqdm

from config import REPO_ROOT
from load_manifest import _find_series_dir
from variants import _det_uid

# ── Parameter ────────────────────────────────────────────────────────────────
SLAB_MM           = 5.0     # Dicke eines Blocks
MIN_REST_MM       = 2.5     # kürzerer Rest oben wird dem obersten Block zugeschlagen
DOWN              = 4       # Verkleinerung für die Flächenmessung (512 -> 128)
BONE_HU           = 250     # Knochen
TISSUE_HU         = (0, 100)
BONE_DILATE       = 2       # Pixel (à ~1,8 mm): schließt kleine Knochenlücken
BONE_DILATE_STARK = 4       # schließt größere Lücken an der Schädelbasis
TOP_MIN_CM2       = 1.0     # oben: weiter, solange noch Gewebe im Schädel
BOTTOM_FRAC       = 0.08    # unten: Fläche ab 8 % des Maximums
SMOOTH_SLICES     = 5       # unten: Median über 5 Schichten
BRIDGE_MM         = 10.0    # unten: Messlücken bis 10 mm überbrücken ...
BRIDGE_MIN_FRAC   = 0.25    # ... nur bei schlagartigem Einbruch (vorher >= 25 %)

# Name der 5-mm-Serie aus dem Namen der dünnen Serie: Hinweis auf die dünne
# Schicht entfällt. Ausnahmen: "PRE CONTRAST" und "" kommen im CQ500-Datensatz
# nicht vor, dort gilt der Name der nativen 5-mm-Serie derselben Untersuchung.
SERIES_NAMES = {
    "PLAIN THIN":        "PLAIN",
    "Thin Plain":        "Plain",
    "Plain THIN":        "Plain",
    "PRE CONTRAST THIN": "PRE CONTRAST 5MM STD",
    "0.625mm":           "5mm",
}


# ── Gehirnbereich ────────────────────────────────────────────────────────────

def _dilate(mask, n):
    for _ in range(n):
        m = mask.copy()
        m[1:, :] |= mask[:-1, :]
        m[:-1, :] |= mask[1:, :]
        m[:, 1:] |= mask[:, :-1]
        m[:, :-1] |= mask[:, 1:]
        mask = m
    return mask


def _inside_skull(bone):
    """Pixel innerhalb des Knochenrings: Flutung von außen durch Nicht-Knochen."""
    outside = np.zeros_like(bone)
    outside[0, :] = outside[-1, :] = outside[:, 0] = outside[:, -1] = True
    outside &= ~bone
    while True:
        grown = outside.copy()
        grown[1:, :] |= outside[:-1, :]
        grown[:-1, :] |= outside[1:, :]
        grown[:, 1:] |= outside[:, :-1]
        grown[:, :-1] |= outside[:, 1:]
        grown &= ~bone
        if (grown == outside).all():
            return ~bone & ~outside
        outside = grown


def brain_area(vol, spacing_mm):
    """Fläche von Gewebe innerhalb des Schädels pro Schicht in cm²."""
    areas = []
    for hu in vol:
        h, w = hu.shape[0] // DOWN * DOWN, hu.shape[1] // DOWN * DOWN
        blocks = hu[:h, :w].reshape(h // DOWN, DOWN, w // DOWN, DOWN)
        small = blocks.mean(axis=(1, 3))
        # Knochen über das Maximum, damit dünne Schädelbasis nicht wegmittelt.
        # Zwei Stärken beim Schließen von Lücken: schwach ist oben genau, stark
        # dichtet größere Lücken an der Schädelbasis ab. Es zählt der größere Wert.
        bone = blocks.max(axis=(1, 3)) > BONE_HU
        gewebe = (small >= TISSUE_HU[0]) & (small <= TISSUE_HU[1])
        flaeche = max(
            (_inside_skull(_dilate(bone, d)) & gewebe).sum()
            for d in (BONE_DILATE, BONE_DILATE_STARK)
        )
        areas.append(flaeche * (spacing_mm * DOWN) ** 2 / 100.0)
    return np.array(areas)


def brain_range(areas, zs):
    """Index der untersten und obersten Schicht des Gehirnbereichs."""
    peak = int(np.argmax(areas))

    top = peak
    while top + 1 < len(areas) and areas[top + 1] >= TOP_MIN_CM2:
        top += 1

    # Unten: Median über mehrere Schichten, damit einzelne Schichten mit Lücke
    # im Knochen der Schädelbasis die Suche nicht abbrechen
    r = SMOOTH_SLICES // 2
    pad = np.pad(areas, r, mode="edge")
    smooth = np.median(np.stack([pad[i:i + len(areas)] for i in range(SMOOTH_SLICES)]), axis=0)

    schwelle = BOTTOM_FRAC * areas[peak]
    bottom = peak
    i = peak - 1
    while i >= 0:
        if smooth[i] >= schwelle:
            bottom = i
            i -= 1
            continue
        # Am normalen unteren Ende läuft die Fläche aus; eine Messlücke bricht
        # dagegen mitten im Gehirn schlagartig ein. Nur diese wird überbrückt.
        if smooth[bottom] < BRIDGE_MIN_FRAC * areas[peak]:
            break
        weiter = [j for j in range(i - 1, -1, -1)
                  if zs[bottom] - zs[j] <= BRIDGE_MM and smooth[j] >= schwelle]
        if not weiter:
            break
        i = weiter[0]
    return bottom, top


def slabs(zs, bottom, top):
    """Lückenlose 5-mm-Blöcke von unten nach oben als Listen von Schichtindizes."""
    idx = np.arange(len(zs))
    grenzen = []
    start = zs[bottom]
    while start <= zs[top]:
        grenzen.append(start)
        start += SLAB_MM
    if len(grenzen) > 1 and zs[top] - grenzen[-1] < MIN_REST_MM:
        grenzen.pop()
    out = []
    for k, start in enumerate(grenzen):
        ende = grenzen[k + 1] if k + 1 < len(grenzen) else np.inf
        sel = idx[(zs >= start) & (zs < ende) & (idx >= bottom) & (idx <= top)]
        if len(sel):
            out.append(sel)
    return out


# ── DICOM ────────────────────────────────────────────────────────────────────

def series_name(thin_name: str) -> str:
    if thin_name not in SERIES_NAMES:
        raise ValueError(f"Kein 5-mm-Name für dünne Serie {thin_name!r} festgelegt")
    return SERIES_NAMES[thin_name]


def slab_dataset(template, mean_hu, k, n_slabs, slab_z, slab_idx, zs, source_uid, name):
    """DICOM-Bild eines 5-mm-Blocks. Kopf aus der mittleren dünnen Schicht."""
    ds = copy.deepcopy(template)
    series_uid = _det_uid(f"5mm|{source_uid}")
    sop_uid = _det_uid(f"5mm|{source_uid}|{k}")

    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SeriesDescription = name
    ds.InstanceNumber = k
    ds.ImageType = ["DERIVED", "SECONDARY", "AXIAL"]
    dicke = float(zs[slab_idx[-1]] - zs[slab_idx[0]]) + float(np.median(np.diff(zs)))
    ds.SliceThickness = f"{dicke:.2f}"
    if "SpacingBetweenSlices" in ds:
        ds.SpacingBetweenSlices = f"{SLAB_MM:.2f}"
    pos = [float(v) for v in ds.ImagePositionPatient]
    ds.ImagePositionPatient = [pos[0], pos[1], round(float(slab_z), 3)]
    if "SliceLocation" in ds:
        ds.SliceLocation = round(float(slab_z), 3)
    ds.DerivationDescription = (
        f"Mean of {len(slab_idx)} thin slices (z {zs[slab_idx[0]]:.2f} to "
        f"{zs[slab_idx[-1]]:.2f} mm), block {k} of {n_slabs}"
    )

    slope = float(ds.get("RescaleSlope", 1) or 1)
    inter = float(ds.get("RescaleIntercept", 0) or 0)
    stored = np.round((mean_hu - inter) / slope)
    info = np.iinfo(np.int16 if ds.PixelRepresentation == 1 else np.uint16)
    stored = np.clip(stored, info.min, info.max).astype(info.dtype)
    ds.PixelData = stored.tobytes()
    ds["PixelData"].VR = "OW"
    ds["PixelData"].is_undefined_length = False
    return ds


def load_thin(series_dir: Path, sop_uids: set):
    """Dünne Schichten der Serie, nach z sortiert: (z, HU, Dataset)."""
    data = []
    for f in sorted(series_dir.iterdir()):
        if not f.is_file():
            continue
        ds = pydicom.dcmread(f)
        if str(ds.SOPInstanceUID) not in sop_uids:
            continue
        hu = ds.pixel_array.astype(np.float32) * float(ds.RescaleSlope) + float(ds.RescaleIntercept)
        data.append((float(ds.ImagePositionPatient[2]), hu, ds))
    data.sort(key=lambda t: t[0])
    return data


def build_case(entry: dict, dicom_dir: Path, out_dir: Path) -> dict:
    pid = entry["PatientID"]
    series_dir = _find_series_dir(dicom_dir / pid, entry["SeriesInstanceUID"])
    if series_dir is None:
        raise FileNotFoundError(f"{pid}: dünne Serie nicht gefunden")
    data = load_thin(series_dir, set(entry["SOPInstanceUIDs"]))
    if len(data) != len(entry["SOPInstanceUIDs"]):
        raise ValueError(f"{pid}: {len(data)} von {len(entry['SOPInstanceUIDs'])} Schichten gefunden")

    zs = np.array([d[0] for d in data])
    vol = np.stack([d[1] for d in data])
    spacing = float(data[0][2].PixelSpacing[0])
    thin_name = str(data[0][2].SeriesDescription)
    name = series_name(thin_name)

    areas = brain_area(vol, spacing)
    bottom, top = brain_range(areas, zs)
    blocks = slabs(zs, bottom, top)

    ziel = out_dir / pid
    ziel.mkdir(parents=True, exist_ok=True)
    for alt in ziel.glob("*.dcm"):
        alt.unlink()

    sop_uids, slab_info = [], []
    for k, sel in enumerate(blocks, 1):
        template = data[sel[len(sel) // 2]][2]
        slab_z = float(zs[sel].mean())
        ds = slab_dataset(template, vol[sel].mean(axis=0), k, len(blocks), slab_z, sel, zs,
                          entry["SeriesInstanceUID"], name)
        ds.save_as(ziel / f"{k:03d}.dcm", enforce_file_format=True)
        sop_uids.append(str(ds.SOPInstanceUID))
        slab_info.append({"z_center": round(slab_z, 3),
                          "thin_slices": [int(sel[0]), int(sel[-1])]})

    return {
        "PatientID": pid,
        "StudyInstanceUID": entry["StudyInstanceUID"],
        "SeriesInstanceUID": _det_uid(f"5mm|{entry['SeriesInstanceUID']}"),
        "SeriesDescription": name,
        "SOPInstanceUIDs": sop_uids,
        "Source": {
            "SeriesInstanceUID": entry["SeriesInstanceUID"],
            "SeriesDescription": thin_name,
            "n_slices": len(zs),
            "spacing_mm": round(float(np.median(np.diff(zs))), 3),
            "brain_range_slices": [int(bottom), int(top)],
            "brain_range_z_mm": [round(float(zs[bottom]), 2), round(float(zs[top]), 2)],
        },
        "Slabs": slab_info,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=REPO_ROOT / "orthanc_80_manifest.json", type=Path)
    ap.add_argument("--dicom-dir", default=REPO_ROOT / "cq-500-dicoms", type=Path)
    ap.add_argument("--out-dir", default=REPO_ROOT / "cq-500-5mm", type=Path)
    ap.add_argument("--out-manifest", default=REPO_ROOT / "orthanc_80_5mm_manifest.json", type=Path)
    ap.add_argument("--only", nargs="*", help="nur diese PatientIDs (Manifest wird nicht geschrieben)")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    if args.only:
        manifest = [e for e in manifest if e["PatientID"] in set(args.only)]

    ergebnis = []
    for entry in tqdm(manifest, desc="Fälle"):
        ergebnis.append(build_case(entry, args.dicom_dir, args.out_dir))

    for e in ergebnis:
        s = e["Source"]
        print(f"{e['PatientID']}: {s['SeriesDescription']!r} -> {e['SeriesDescription']!r}, "
              f"{len(e['SOPInstanceUIDs'])} Blöcke, Schichten {s['brain_range_slices']}")

    if args.only:
        print("Teilauswahl: Manifest nicht geschrieben.")
        return
    text = json.dumps(ergebnis, indent=2).replace("\n", "\r\n")
    args.out_manifest.write_bytes(text.encode())
    print(f"Manifest: {args.out_manifest} ({len(ergebnis)} Fälle)")


if __name__ == "__main__":
    sys.exit(main())
