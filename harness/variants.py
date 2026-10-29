"""
variants.py
-----------
Materialisiert manipulierte Angriffsvarianten als eigene DICOM-Studien in Orthanc.

Das Original wird dabei nie verändert: es wird ausschließlich gelesen
(/studies/{id}/archive) und als Kopie neu hochgeladen. Jede Kopie erhält

  - eine eigene StudyInstanceUID
  - neue SeriesInstanceUIDs pro Serie
  - neue SOPInstanceUIDs pro Instanz
  - eine eigene PatientID nach dem Schema <Studie>__<Attack-ID>

Dadurch ist die Variante in Orthanc eindeutig adressierbar und über die
PatientID auffindbar. Wiederholungen (rep 0/1/2) verwenden dieselbe Kopie
wieder, statt sie neu zu erzeugen.

Unterstützte Platzierungen:
  StudyDescription | SeriesDescription | ImageComments | Bild
"Vorbefund" verändert keine DICOM-Daten (der Text geht in den Prompt ein) und
erzeugt daher keine Kopie.
"""

import io
import hashlib
import zipfile

import numpy as np
import pydicom
import requests
from pydicom.uid import ExplicitVRLittleEndian, RLELossless

from config import ORTHANC_URL, ORTHANC_USER, ORTHANC_PASS, VARIANT_SEPARATOR

# Platzierungen, die tatsächlich DICOM-Daten verändern -> eigene Kopie nötig
DICOM_PLACEMENTS = {"StudyDescription", "SeriesDescription", "ImageComments", "Bild"}

# Schriftdateien für eingebrannten Text (erste vorhandene wird genutzt)
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",      # macOS
    "/System/Library/Fonts/Helvetica.ttc",               # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",   # Linux
)


# ── Helfer ────────────────────────────────────────────────────────────────────

def _auth():
    return (ORTHANC_USER, ORTHANC_PASS) if ORTHANC_USER else None


def _det_uid(key: str) -> str:
    """
    Deterministische DICOM-UID aus einem Schlüssel ("2.25." + 128 Bit).
    Gleiche Variante -> gleiche UID, auch nach einem Neuaufbau von Orthanc.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return "2.25." + str(int.from_bytes(digest[:16], "big"))


def variant_patient_id(study_id: str, attack_id: str) -> str:
    """PatientID der Kopie, z. B. 'CQ500-CT-10__A33'."""
    return f"{study_id}{VARIANT_SEPARATOR}{attack_id}"


def is_variant_id(patient_id: str) -> bool:
    return VARIANT_SEPARATOR in (patient_id or "")


def _patient_id(study_info: dict) -> str:
    pid = study_info.get("PatientMainDicomTags", {}).get("PatientID", "")
    return pid or study_info.get("MainDicomTags", {}).get("PatientID", "")


def _find_by_patient(patient_id: str):
    """Sucht eine Studie über die PatientID. Gibt (orthanc_id, info) zurück."""
    if not patient_id:
        return None, None

    # Bevorzugt: Orthanc-Index
    try:
        resp = requests.post(
            f"{ORTHANC_URL}/tools/find",
            json={"Level": "Study", "Query": {"PatientID": patient_id}},
            auth=_auth(),
            timeout=15,
        )
        if resp.status_code == 200:
            ids = resp.json()
            if ids:
                info = requests.get(
                    f"{ORTHANC_URL}/studies/{ids[0]}", auth=_auth(), timeout=10
                ).json()
                return ids[0], info
    except Exception:
        pass

    # Fallback: linear durchsuchen
    for sid in requests.get(f"{ORTHANC_URL}/studies", auth=_auth(), timeout=15).json():
        info = requests.get(f"{ORTHANC_URL}/studies/{sid}", auth=_auth(), timeout=10).json()
        if _patient_id(info) == patient_id:
            return sid, info
    return None, None


def _find_original(study_id: str):
    """
    Sucht das unveränderte Original. Varianten (PatientID mit '__') werden
    ausgeschlossen, damit nicht versehentlich eine Kopie als Original dient.
    """
    sid, info = _find_by_patient(study_id)
    if info and not is_variant_id(_patient_id(info)):
        return sid, info

    for cand in requests.get(f"{ORTHANC_URL}/studies", auth=_auth(), timeout=15).json():
        info = requests.get(f"{ORTHANC_URL}/studies/{cand}", auth=_auth(), timeout=10).json()
        pid = _patient_id(info)
        if pid and not is_variant_id(pid) and study_id in pid:
            return cand, info
    return None, None


def _font(size: int = 11):
    from PIL import ImageFont
    import os
    for path in _FONT_CANDIDATES:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # ältere Pillow-Versionen
        return ImageFont.load_default()


def _burn_text_into(ds, text: str) -> None:
    """
    Brennt Text in die Pixel ein (wie Clusmann et al. 2025): schwarzer Balken am
    unteren Rand, weißer Text. Die Werte werden in gespeicherten Grauwerten
    gesetzt, also über RescaleSlope/Intercept in HU umgerechnet.
    """
    from PIL import Image, ImageDraw

    arr = ds.pixel_array
    if arr.ndim != 2:
        raise ValueError(f"nur Einzelbilder unterstützt (Form: {arr.shape})")

    orig_dtype = arr.dtype
    arr = arr.astype(np.int32, copy=True)
    h, w = arr.shape

    slope = float(ds.get("RescaleSlope", 1) or 1)
    inter = float(ds.get("RescaleIntercept", 0) or 0)
    limits = np.iinfo(orig_dtype if np.issubdtype(orig_dtype, np.integer) else np.int16)

    def to_stored(hu: float) -> int:
        value = int(round((hu - inter) / slope)) if slope else int(hu)
        return max(int(limits.min), min(int(limits.max), value))

    black = to_stored(-1024.0)   # entspricht Luft/Hintergrund im Hirnfenster
    white = to_stored(2000.0)    # deutlich über dem Fenster -> weiß

    font = _font(11)
    bar_img = Image.new("L", (w, h), 0)
    txt_img = Image.new("L", (w, h), 0)
    txt_draw = ImageDraw.Draw(txt_img)

    # Text auf die Bildbreite umbrechen, damit nichts abgeschnitten wird
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if txt_draw.textlength(candidate, font=font) <= w - 8 or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    lines = lines[:4]

    line_h = max(13, h // 34)
    bar_h = max(18, line_h * len(lines) + 6)

    ImageDraw.Draw(bar_img).rectangle([(0, h - bar_h), (w - 1, h - 1)], fill=255)
    for i, text_line in enumerate(lines):
        txt_draw.text(
            (4, h - bar_h + 3 + i * line_h), text_line, fill=255, font=font
        )

    arr[np.array(bar_img) > 0] = black
    arr[np.array(txt_img) > 0] = white

    # Verlustfrei neu kodieren (RLE). JPEG-Lossless kann pydicom nicht schreiben.
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds["PixelData"].is_undefined_length = False
    ds.PixelData = arr.astype(limits.dtype).tobytes()
    ds.compress(RLELossless)


def _apply_placement(ds, placement: str, text: str) -> None:
    """Setzt den Angriffstext in das passende DICOM-Feld."""
    if placement == "StudyDescription":
        ds.StudyDescription = text
    elif placement == "SeriesDescription":
        ds.SeriesDescription = text
    elif placement == "ImageComments":
        ds.ImageComments = text
    elif placement == "Bild":
        _burn_text_into(ds, text)
    else:
        raise ValueError(f"nicht unterstützte Platzierung: {placement}")


# ── Öffentliche API ──────────────────────────────────────────────────────────

def _attack_text(attack: dict) -> str:
    """
    Wählt den Angriffstext wie der Agent: Kurztexte für SD/SE,
    Langtexte für ImageComments und Bild.
    """
    placement = (attack.get("placement") or "").strip()
    if placement in ("ImageComments", "Vorbefund", "Bild"):
        return attack.get("long_text") or attack.get("short_text") or ""
    return attack.get("short_text") or attack.get("long_text") or ""


def ensure_variant(
    study_id: str,
    attack_id: str,
    attack: dict,
    text: str | None = None,
    force: bool = False,
    verbose: bool = True,
    limit: int | None = None,
) -> dict | None:
    """
    Stellt sicher, dass die manipulierte Variante als eigene Studie in Orthanc
    liegt, und gibt ihre Metadaten zurück.

    Rückgabe (dict) oder None, wenn die Platzierung keine DICOM-Kopie braucht:
        {
          "patient_id":         "CQ500-CT-10__A33",
          "orthanc_id":         "<study id>",
          "study_instance_uid": "2.25....",
          "placement":          "Bild",
          "n_instances":        224,
          "created":            False,
          "origin_orthanc_id":  "<study id des Originals>",
        }

    force=True löscht eine bestehende Kopie vorher.
    limit begrenzt die Zahl der Instanzen (nur für Tests gedacht).
    """
    placement = (attack.get("placement") or "").strip()
    if placement not in DICOM_PLACEMENTS:
        return None

    text = text if text is not None else _attack_text(attack)
    vpid = variant_patient_id(study_id, attack_id)

    # Bestehende Kopie wiederverwenden
    existing_id, existing_info = _find_by_patient(vpid)
    if existing_id and not force:
        return {
            "patient_id":         vpid,
            "orthanc_id":         existing_id,
            "study_instance_uid": existing_info.get("MainDicomTags", {}).get("StudyInstanceUID", ""),
            "placement":          placement,
            "n_instances":        _count_instances(existing_info),
            "created":            False,
            "origin_orthanc_id":  None,
        }
    if existing_id and force:
        requests.delete(f"{ORTHANC_URL}/studies/{existing_id}", auth=_auth(), timeout=30)

    # Original lesen (nie schreiben)
    origin_id, _ = _find_original(study_id)
    if not origin_id:
        raise RuntimeError(
            f"Originalstudie '{study_id}' nicht in Orthanc gefunden "
            f"({ORTHANC_URL}). Variante kann nicht erzeugt werden."
        )

    if verbose:
        print(f"[variant] erzeuge {vpid} aus Original {origin_id[:8]} ({placement}) ...")

    archive = requests.get(
        f"{ORTHANC_URL}/studies/{origin_id}/archive", auth=_auth(), timeout=120
    )
    archive.raise_for_status()

    study_uid = _det_uid(f"{study_id}|{attack_id}|study")
    series_uids: dict[str, str] = {}
    uploaded = 0
    failed = 0

    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        if limit:
            names = names[:limit]

        session = requests.Session()
        for name in names:
            try:
                ds = pydicom.dcmread(io.BytesIO(zf.read(name)))

                # Neue Identität für die Kopie
                old_series = str(ds.SeriesInstanceUID)
                if old_series not in series_uids:
                    series_uids[old_series] = _det_uid(
                        f"{study_id}|{attack_id}|series|{old_series}"
                    )
                ds.StudyInstanceUID  = study_uid
                ds.SeriesInstanceUID = series_uids[old_series]
                ds.SOPInstanceUID    = _det_uid(
                    f"{study_id}|{attack_id}|sop|{ds.SOPInstanceUID}"
                )
                ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
                ds.file_meta.MediaStorageSOPClassUID    = ds.SOPClassUID

                # Eindeutige Patienten-/Studienzuordnung
                ds.PatientID = vpid
                ds.StudyID   = f"VARIANT-{attack_id}"

                # Eigentliche Manipulation
                _apply_placement(ds, placement, text)

                buf = io.BytesIO()
                ds.save_as(buf, enforce_file_format=True)

                resp = session.post(
                    f"{ORTHANC_URL}/instances",
                    data=buf.getvalue(),
                    headers={"Content-Type": "application/dicom"},
                    auth=_auth(),
                    timeout=60,
                )
                if resp.status_code in (200, 201):
                    uploaded += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                if verbose:
                    print(f"[variant]  Instanz {name} übersprungen: {exc}")

    if uploaded == 0:
        raise RuntimeError(f"Keine Instanz für Variante '{vpid}' hochgeladen ({failed} Fehler)")

    new_id, new_info = _find_by_patient(vpid)
    if not new_id:
        raise RuntimeError(f"Variante '{vpid}' nach dem Upload nicht in Orthanc auffindbar")

    if verbose:
        print(f"[variant] fertig: {vpid} ({uploaded} Instanzen"
              f"{f', {failed} Fehler' if failed else ''})")

    return {
        "patient_id":         vpid,
        "orthanc_id":         new_id,
        "study_instance_uid": study_uid,
        "placement":          placement,
        "n_instances":        uploaded,
        "created":            True,
        "origin_orthanc_id":  origin_id,
    }


def _count_instances(study_info: dict) -> int:
    total = 0
    for series_id in study_info.get("Series", []):
        try:
            inst = requests.get(
                f"{ORTHANC_URL}/series/{series_id}/instances", auth=_auth(), timeout=15
            ).json()
            total += len(inst)
        except Exception:
            continue
    return total


def list_variants(study_id: str | None = None) -> list[dict]:
    """Listet vorhandene Varianten in Orthanc (optional nur für eine Studie)."""
    out = []
    for sid in requests.get(f"{ORTHANC_URL}/studies", auth=_auth(), timeout=20).json():
        info = requests.get(f"{ORTHANC_URL}/studies/{sid}", auth=_auth(), timeout=10).json()
        pid = _patient_id(info)
        if not is_variant_id(pid):
            continue
        if study_id and not pid.startswith(study_id + VARIANT_SEPARATOR):
            continue
        out.append({
            "patient_id": pid,
            "orthanc_id": sid,
            "study_instance_uid": info.get("MainDicomTags", {}).get("StudyInstanceUID", ""),
        })
    return out
