"""
Offline-Tests für build_5mm_series.py: Gehirnbereich, 5-mm-Blöcke,
Seriennamen und DICOM-Kopf der gemittelten Bilder.

Kein Orthanc, keine Dateien aus cq-500-dicoms: Volumen und Flächenkurven sind
künstlich. Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ImplicitVRLittleEndian

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_5mm_series as b

ZS = np.arange(0, 120, 0.625)          # 192 dünne Schichten


def flaechen(profil):
    """Flächenkurve aus Stützstellen [(z, cm²), ...], linear interpoliert."""
    z, a = zip(*profil)
    return np.interp(ZS, z, a)


class TestBloecke(unittest.TestCase):

    def test_lueckenlos_ohne_ueberlappung(self):
        bottom, top = 16, 180
        blocks = b.slabs(ZS, bottom, top)
        alle = np.concatenate(blocks)
        self.assertEqual(list(alle), list(range(bottom, top + 1)))
        for sel in blocks[:-1]:
            self.assertEqual(len(sel), 8)               # 5 mm / 0,625 mm

    def test_rest_wird_zugeschlagen(self):
        # Bereich 0 .. 101,875 mm: 20 volle Blöcke, Rest 1,875 mm < 2,5 mm
        # geht in den obersten Block (8 + 4 Schichten)
        blocks = b.slabs(ZS, 0, 163)
        self.assertEqual(len(blocks), 20)
        self.assertEqual(len(blocks[-1]), 12)

    def test_rest_ab_2_5_mm_bleibt_eigener_block(self):
        # Bereich 0 .. 103,125 mm: Rest 3,125 mm
        blocks = b.slabs(ZS, 0, 165)
        self.assertEqual(len(blocks), 21)
        self.assertEqual(len(blocks[-1]), 6)


class TestGrenzen(unittest.TestCase):

    def test_oben_bis_nur_noch_kalotte(self):
        a = flaechen([(0, 0), (20, 30), (60, 150), (100, 20), (108, 1.5), (110, 0.2), (120, 0)])
        _, top = b.brain_range(a, ZS)
        self.assertGreaterEqual(a[top], b.TOP_MIN_CM2)
        self.assertLess(a[top + 1], b.TOP_MIN_CM2)

    def test_unten_normales_auslaufen(self):
        a = flaechen([(0, 2), (10, 5), (20, 30), (60, 150), (110, 0), (120, 0)])
        bottom, _ = b.brain_range(a, ZS)
        self.assertGreaterEqual(a[bottom], b.BOTTOM_FRAC * 150 - 1)
        self.assertTrue(13 <= ZS[bottom] <= 15)

    def test_messluecke_wird_ueberbrueckt(self):
        """Schlagartiger Einbruch mitten in der hinteren Schädelgrube."""
        a = flaechen([(0, 2), (8, 5), (14, 40), (60, 150), (110, 0), (120, 0)])
        luecke = (ZS >= 22) & (ZS < 28)                 # 6 mm Fläche 0
        a[luecke] = 0
        bottom, _ = b.brain_range(a, ZS)
        self.assertLess(ZS[bottom], 22)

    def test_kein_sprung_in_den_hals(self):
        """Auslaufen bis knapp über die Schwelle, darunter wieder Fläche (Hals)."""
        a = flaechen([(0, 20), (6, 20), (8, 0), (12, 0), (14, 13), (20, 30),
                      (60, 150), (110, 0), (120, 0)])
        bottom, _ = b.brain_range(a, ZS)
        self.assertGreater(ZS[bottom], 12)

    def test_luecke_ueber_10_mm_nicht_ueberbrueckt(self):
        a = flaechen([(0, 2), (8, 40), (60, 150), (110, 0), (120, 0)])
        a[(ZS >= 20) & (ZS < 32)] = 0                   # 12 mm
        bottom, _ = b.brain_range(a, ZS)
        self.assertGreaterEqual(ZS[bottom], 32)


class TestFlaeche(unittest.TestCase):

    PIXEL_MM = 0.45                                     # wie im CQ500-Datensatz
    HIRN_MM, KNOCHEN_MM = 70.0, 7.0

    def kopf(self, luecke_mm=0.0):
        """Schicht 512x512: Hirngewebe (35 HU) in einem Knochenring (1000 HU)."""
        y, x = np.mgrid[:512, :512]
        r = np.hypot(x - 256, y - 256) * self.PIXEL_MM
        hu = np.full((512, 512), -1000.0)
        hu[r < self.HIRN_MM + self.KNOCHEN_MM] = 1000.0
        hu[r < self.HIRN_MM] = 35.0
        if luecke_mm:
            halb = int(luecke_mm / self.PIXEL_MM / 2)
            hu[256 - halb:256 + halb, 256:][r[256 - halb:256 + halb, 256:] >= self.HIRN_MM] = 35.0
        return hu

    def flaeche(self, luecke_mm=0.0):
        return b.brain_area(np.stack([self.kopf(luecke_mm)]), self.PIXEL_MM)[0]

    def test_geschlossener_ring(self):
        """Das Schließen macht den Knochen nach innen dicker: etwas weniger Fläche."""
        wahr = np.pi * (self.HIRN_MM / 10) ** 2
        self.assertTrue(0.85 * wahr < self.flaeche() <= wahr)

    def test_kleine_luecke_schon_schwach_geschlossen(self):
        self.assertAlmostEqual(self.flaeche(5.0), self.flaeche(), delta=2)

    def test_14_mm_luecke_nur_stark_geschlossen(self):
        """Schwach schließt bis etwa 10 mm, stark bis etwa 16 mm."""
        voll = self.flaeche()
        self.assertGreater(self.flaeche(14.0), 0.8 * voll)
        # Nur mit schwachem Schließen liefe die Füllung hinein
        alt = b.BONE_DILATE_STARK
        try:
            b.BONE_DILATE_STARK = b.BONE_DILATE
            self.assertLess(self.flaeche(14.0), 0.1 * voll)
        finally:
            b.BONE_DILATE_STARK = alt

    def test_grosse_luecke_bleibt_offen(self):
        self.assertLess(self.flaeche(25.0), 0.1 * self.flaeche())


class TestSerienname(unittest.TestCase):

    def test_zuordnung(self):
        self.assertEqual(b.series_name("PLAIN THIN"), "PLAIN")
        self.assertEqual(b.series_name("Thin Plain"), "Plain")
        self.assertEqual(b.series_name("Plain THIN"), "Plain")
        self.assertEqual(b.series_name("PRE CONTRAST THIN"), "PRE CONTRAST 5MM STD")
        self.assertEqual(b.series_name("0.625mm"), "5mm")

    def test_unbekannter_name(self):
        with self.assertRaises(ValueError):
            b.series_name("Thin Bone")

    def test_namen_in_allowlist(self):
        from sanitize import sanitize_series_description
        for name in b.SERIES_NAMES.values():
            self.assertEqual(sanitize_series_description(name), name)


class TestDicom(unittest.TestCase):

    def vorlage(self):
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
        ds.SOPClassUID = CTImageStorage
        ds.PatientID = "CQ500-CT-1"
        ds.StudyInstanceUID = "1.2.3"
        ds.SeriesInstanceUID = "1.2.3.4"
        ds.SOPInstanceUID = "1.2.3.4.5"
        ds.SeriesDescription = "PLAIN THIN"
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.ImagePositionPatient = [-100, -120, 7.5]
        ds.SliceThickness = "0.625"
        ds.RescaleSlope = 1
        ds.RescaleIntercept = -1024
        ds.Rows = ds.Columns = 4
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = np.zeros((4, 4), np.int16).tobytes()
        return ds

    def test_kopf_und_pixel(self):
        tpl = self.vorlage()
        mean_hu = np.full((4, 4), 35.4)
        zs = np.arange(0, 10, 0.625)
        sel = np.arange(4, 12)
        ds = b.slab_dataset(tpl, mean_hu, 3, 20, zs[sel].mean(), sel, zs, "1.2.3.4", "PLAIN")

        self.assertEqual(ds.StudyInstanceUID, "1.2.3")
        self.assertEqual(ds.SeriesInstanceUID, b._det_uid("5mm|1.2.3.4"))
        self.assertEqual(ds.SOPInstanceUID, b._det_uid("5mm|1.2.3.4|3"))
        self.assertEqual(ds.file_meta.MediaStorageSOPInstanceUID, ds.SOPInstanceUID)
        self.assertEqual(ds.SeriesDescription, "PLAIN")
        self.assertEqual(ds.InstanceNumber, 3)
        self.assertEqual(list(ds.ImageType), ["DERIVED", "SECONDARY", "AXIAL"])
        self.assertAlmostEqual(float(ds.SliceThickness), 5.0)
        self.assertAlmostEqual(float(ds.ImagePositionPatient[2]), zs[sel].mean(), places=3)
        self.assertIn("Mean of 8 thin slices", ds.DerivationDescription)
        hu = ds.pixel_array * float(ds.RescaleSlope) + float(ds.RescaleIntercept)
        self.assertTrue((hu == 35).all())               # auf ganze HU gerundet
        # Vorlage bleibt unverändert
        self.assertEqual(tpl.SOPInstanceUID, "1.2.3.4.5")
        self.assertEqual(tpl.SeriesDescription, "PLAIN THIN")

    def test_uids_reproduzierbar(self):
        zs = np.arange(0, 10, 0.625)
        sel = np.arange(0, 8)
        a = b.slab_dataset(self.vorlage(), np.zeros((4, 4)), 1, 2, 2.0, sel, zs, "9.9", "Plain")
        c = b.slab_dataset(self.vorlage(), np.zeros((4, 4)), 1, 2, 2.0, sel, zs, "9.9", "Plain")
        self.assertEqual(a.SOPInstanceUID, c.SOPInstanceUID)
        self.assertEqual(a.SeriesInstanceUID, c.SeriesInstanceUID)


class TestViewSlices(unittest.TestCase):
    """view_slices nimmt die eine Serie der Studie und zeigt alle Bilder."""

    def lauf(self, n_bilder, series_ids=("ser1",), args=None, ctx=None):
        import io
        from types import SimpleNamespace
        from unittest import mock
        from PIL import Image
        import tools

        buf = io.BytesIO()
        Image.new("L", (512, 512)).save(buf, format="PNG")
        abgerufen = []

        def get(url, **kw):
            if url.endswith("/series/ser1"):
                return SimpleNamespace(json=lambda: {"MainDicomTags": {"SeriesDescription": "PLAIN"}})
            abgerufen.append(url.split("/instances/")[1].split("/")[0])
            return SimpleNamespace(status_code=200, content=buf.getvalue())

        with mock.patch.object(tools, "_find_study", return_value=({"ID": "st"}, list(series_ids))), \
             mock.patch.object(tools, "_instance_ids", return_value=[f"i{k}" for k in range(n_bilder)]), \
             mock.patch.object(tools.requests, "get", side_effect=get):
            res = tools._view_slices(args or {"study_id": "CQ500-CT-1"}, ctx or {})
        return res, abgerufen

    def test_alle_bilder_in_reihenfolge(self):
        res, abgerufen = self.lauf(26)
        self.assertEqual(res["n_slices"], 26)
        self.assertEqual(len(res["images"]), 26)
        self.assertEqual(res["series"], "PLAIN")
        self.assertEqual(abgerufen, [f"i{k}" for k in range(26)])

    def test_serienangabe_spielt_keine_rolle(self):
        res, _ = self.lauf(24, args={"study_id": "CQ500-CT-1", "series": "Plain Thin"})
        self.assertEqual(res["n_slices"], 24)
        self.assertEqual(res["series"], "PLAIN")

    def test_obergrenze(self):
        from config import MAX_SLICES
        res, abgerufen = self.lauf(MAX_SLICES + 10)
        self.assertEqual(res["n_slices"], MAX_SLICES)
        self.assertEqual(abgerufen[0], "i0")

    def test_nicht_genau_eine_serie(self):
        res, _ = self.lauf(24, series_ids=("ser1", "ser2"))
        self.assertIn("error", res)
        self.assertIn("exactly one series", res["error"])

    def test_tool_beschreibung(self):
        import tools
        teil = tools.TOOL_DEFINITIONS.split("3. view_slices")[1].split("4. run_detector")[0]
        self.assertIn("Input: study_id (string)\n", teil)
        self.assertNotIn("series", teil.split("Example")[1])
        self.assertNotIn("up to 12", teil)


if __name__ == "__main__":
    unittest.main()
