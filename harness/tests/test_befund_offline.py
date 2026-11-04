"""
Offline-Tests für die erlaubten Befundwerte (nur yes/no) und dafür, dass
abgelehnte write_report-/set_priority-Aufrufe nicht als erledigt zählen.

Kein Modell, keine API-Aufrufe, kein Orthanc. Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tools
from config import SYSTEM_PROMPTS
from test_guard_offline import tool
from test_tool_calls_offline import PRIORITY, REPORT, lauf

UNSICHER = tool("write_report", study_id="CQ500-CT-10", hemorrhage="uncertain",
                subtype=None, impression="x", remarks="")


class TestTool(unittest.TestCase):

    def test_yes_und_no_erlaubt(self):
        for wert in ("yes", "no"):
            with self.subTest(wert=wert):
                res = tools._write_report({"hemorrhage": wert, "impression": "x"}, {})
                self.assertEqual(res["status"], "confirmed")

    def test_uncertain_abgelehnt(self):
        for wert in ("uncertain", "Yes", "", None):
            with self.subTest(wert=wert):
                res = tools._write_report({"hemorrhage": wert, "impression": "x"}, {})
                self.assertEqual(res, {"error": "hemorrhage must be 'yes' or 'no'"})

    def test_prompts_und_beschreibung_ohne_uncertain(self):
        for version, text in SYSTEM_PROMPTS.items():
            with self.subTest(version=version):
                self.assertIn("intracranial hemorrhage (yes / no)", text)
                self.assertNotIn("uncertain", text)
        self.assertIn('hemorrhage ("yes"/"no")', tools.TOOL_DEFINITIONS)
        self.assertNotIn("uncertain", tools.TOOL_DEFINITIONS)


class TestAgent(unittest.TestCase):

    def test_korrigierter_befund_zaehlt(self):
        log, _ = lauf([UNSICHER, REPORT, PRIORITY])
        self.assertEqual(log["abort_reason"], "completed")
        self.assertEqual(log["write_report_args"]["hemorrhage"], "yes")
        self.assertEqual(log["n_tool_calls"], 3)
        self.assertIn("error", log["tool_calls"][0]["result"])

    def test_abgelehnter_befund_zaehlt_nicht(self):
        log, _ = lauf([UNSICHER, PRIORITY, "Done."])
        self.assertEqual(log["abort_reason"], "stopped_without_required_tools: ['write_report']")
        self.assertIsNone(log["write_report_args"])

    def test_ungueltige_prioritaet_zaehlt_nicht(self):
        hoch = tool("set_priority", study_id="CQ500-CT-10", priority="high")
        log, _ = lauf([REPORT, hoch, "Done."])
        self.assertEqual(log["abort_reason"], "stopped_without_required_tools: ['set_priority']")
        self.assertIsNone(log["set_priority_args"])


if __name__ == "__main__":
    unittest.main()
