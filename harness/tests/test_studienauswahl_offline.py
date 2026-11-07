"""
Offline-Test: Der Harness läuft nur auf Studien der Stichprobe (Gruppe
positiv_40 / negativ_40 in subset.csv) mit Vorbefund - auch wenn Studien mit
--studies ausdrücklich genannt werden.

Kein Modell, keine API-Aufrufe, kein Orthanc. Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_harness
from config import PRIOR_REPORTS, STICHPROBE_GRUPPEN


class TestStudienauswahl(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.subset = run_harness.load_subset()

    def test_stichprobe_80_mit_vorbefund(self):
        ids = run_harness.select_studies(self.subset, None, None)
        self.assertEqual(len(ids), 80)
        gruppen = [self.subset[s]["Gruppe"] for s in ids]
        self.assertEqual(gruppen.count("positiv_40"), 40)
        self.assertEqual(gruppen.count("negativ_40"), 40)
        self.assertTrue(all((PRIOR_REPORTS / f"{s}.txt").exists() for s in ids))
        # Ground truth passt zur Gruppe
        for s in ids:
            self.assertEqual(self.subset[s]["ICH"] == "1",
                             self.subset[s]["Gruppe"] == "positiv_40", s)

    def test_limit(self):
        self.assertEqual(len(run_harness.select_studies(self.subset, 3, None)), 3)

    def test_explizit_aus_stichprobe(self):
        self.assertEqual(
            run_harness.select_studies(self.subset, None, "CQ500-CT-41, CQ500-CT-58"),
            ["CQ500-CT-41", "CQ500-CT-58"])

    def test_explizit_nicht_ausgewaehlt_bricht_ab(self):
        nicht = next(s for s, r in self.subset.items()
                     if r["Gruppe"] not in STICHPROBE_GRUPPEN)
        with self.assertRaises(SystemExit) as e:
            run_harness.select_studies(self.subset, None, f"CQ500-CT-41,{nicht}")
        self.assertIn(nicht, str(e.exception))
        self.assertIn("nicht in der Stichprobe", str(e.exception))

    def test_explizit_unbekannt_bricht_ab(self):
        with self.assertRaises(SystemExit) as e:
            run_harness.select_studies(self.subset, None, "CQ500-CT-99999")
        self.assertIn("nicht in subset.csv", str(e.exception))

    def test_fehlender_vorbefund_bricht_ab(self):
        with mock.patch.object(run_harness, "PRIOR_REPORTS", Path("/nicht/vorhanden")):
            with self.assertRaises(SystemExit) as e:
                run_harness.select_studies(self.subset, None, "CQ500-CT-41")
            self.assertIn("kein Vorbefund", str(e.exception))
            # auch ohne --studies: kein stilles Überspringen
            with self.assertRaises(SystemExit):
                run_harness.select_studies(self.subset, None, None)


if __name__ == "__main__":
    unittest.main()
