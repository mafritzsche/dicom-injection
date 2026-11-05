"""
Offline-Tests für die festgelegten Modelle (Stand 2026-11-05) und die
Anbindung von OpenRouter und internem Gateway.

Kein Modell, keine API-Aufrufe, kein Orthanc. Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent
import config
from config import PRIOR_REPORTS
from test_guard_offline import ABSCHLUSS, FakeClient

KONTROLLE = {"attack_id": "C06", "placement": "keine", "target": "control",
             "short_text": "", "long_text": "", "applies_to": "both"}


class TestFestgelegteModelle(unittest.TestCase):

    def test_modell_liste(self):
        self.assertEqual(config.MODELS, {
            "anthropic": "anthropic/claude-opus-5.5-20260921",
            "openai":    "openai/gpt-6.1-terra-pro-20261026",
            "google":    "google/gemini-4-argon-20261013",
            "glm":       "glm-5.3-flash",
            "nemotron":  "nemotron-3.5-super-vl",
        })
        self.assertEqual(config.VLLM_MODELS, {"glm", "nemotron"})

    def test_openrouter_datierte_kennung(self):
        for name, kennung in config.MODELS.items():
            if name not in config.VLLM_MODELS:
                with self.subTest(name=name):
                    self.assertRegex(kennung, r"-\d{8}$")


class TestClient(unittest.TestCase):

    def lauf(self, model_name):
        erstellt = []
        antworten = FakeClient(list(ABSCHLUSS))
        original = antworten.create

        def create(**kw):
            r = original(**kw)
            r.model = "geantwortet/" + kw["model"]
            return r
        antworten.create = create

        def client(**kw):
            erstellt.append(kw)
            return antworten

        with mock.patch.object(agent, "OpenAI", client), \
             mock.patch.object(agent, "ensure_variant", return_value=None), \
             mock.patch.object(agent, "_save_log"):
            log = agent.run_single(
                model_name=model_name, study_id="CQ500-CT-10", attack_id="C06",
                attack=KONTROLLE, ground_truth=True, repetition=0,
                prior_report_path=PRIOR_REPORTS / "CQ500-CT-10.txt",
                system_prompt_version="1.0",
            )
        return erstellt[0], log

    def test_gateway_mit_schluessel(self):
        with mock.patch.object(agent, "VLLM_API_KEY", "sk-test"):
            kw, log = self.lauf("glm")
        self.assertEqual(kw["base_url"], config.VLLM_BASE_URL)
        self.assertEqual(kw["api_key"], "sk-test")
        self.assertEqual(log["model_string"], "glm-5.3-flash")

    def test_openrouter(self):
        kw, log = self.lauf("anthropic")
        self.assertEqual(kw["base_url"], config.OPENROUTER_BASE_URL)
        self.assertEqual(log["model_string"], "anthropic/claude-opus-5.5-20260921")

    def test_antwortendes_modell_im_log(self):
        _, log = self.lauf("nemotron")
        self.assertEqual({f["response_model"] for f in log["finish_reasons"]},
                         {"geantwortet/nemotron-3.5-super-vl"})


if __name__ == "__main__":
    unittest.main()
