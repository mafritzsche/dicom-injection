"""
Offline-Test: Die Systemprompts und die Tool-Beschreibung (TOOL_DEFINITIONS)
entsprechen den eingefrorenen Prüfsummen in prompt_hashes.txt. Schlägt fehl,
sobald einer davon geändert wird, ohne dass die Datei bewusst aktualisiert
wird (bei Prompts mit neuer Versionsnummer).

Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from unittest import mock

import agent
from config import PRIOR_REPORTS, REPO_ROOT, SYSTEM_PROMPTS
from tools import TOOL_DEFINITIONS
from test_guard_offline import ABSCHLUSS, FakeClient


def eingefroren() -> dict:
    zeilen = (REPO_ROOT / "prompt_hashes.txt").read_text(encoding="utf-8").splitlines()
    return dict(z.split() for z in zeilen if z.strip() and not z.startswith("#"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestPromptHashes(unittest.TestCase):

    def test_gleiche_versionen(self):
        self.assertEqual(set(eingefroren()) - {"TOOL_DEFINITIONS"}, set(SYSTEM_PROMPTS))

    def test_tool_definitions(self):
        self.assertEqual(sha(TOOL_DEFINITIONS), eingefroren()["TOOL_DEFINITIONS"])

    def test_pruefsummen_im_log(self):
        client = FakeClient(list(ABSCHLUSS))
        kontrolle = {"attack_id": "C06", "placement": "keine", "target": "control",
                     "short_text": "", "long_text": "", "applies_to": "both"}
        with mock.patch.object(agent, "OpenAI", lambda **k: client), \
             mock.patch.object(agent, "ensure_variant", return_value=None), \
             mock.patch.object(agent, "_save_log"):
            log = agent.run_single(
                model_name="anthropic", study_id="CQ500-CT-10", attack_id="C06",
                attack=kontrolle, ground_truth=True, repetition=0,
                prior_report_path=PRIOR_REPORTS / "CQ500-CT-10.txt",
                system_prompt_version="1.1",
            )
        self.assertEqual(log["system_prompt_sha256"], eingefroren()["1.1"])
        self.assertEqual(log["tool_definitions_sha256"], eingefroren()["TOOL_DEFINITIONS"])
        # Die System-Nachricht besteht genau aus Prompt und Tool-Beschreibung
        self.assertEqual(log["messages"][0]["content"],
                         SYSTEM_PROMPTS["1.1"] + "\n\n" + TOOL_DEFINITIONS)

    def test_pruefsummen(self):
        for version, text in SYSTEM_PROMPTS.items():
            with self.subTest(version=version):
                self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                 eingefroren()[version])

    def test_12_gleich_10(self):
        self.assertEqual(eingefroren()["1.2"], eingefroren()["1.0"])


if __name__ == "__main__":
    unittest.main()
