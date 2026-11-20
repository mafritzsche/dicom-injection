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
            "sonnet":    "anthropic/claude-sonnet-5.5-20260928",
            "gpt":       "openai/gpt-6.1-sol-20260929",
            "google":    "google/gemini-4-argon-20261013",
            "glm":       "glm-5.3-flash",
            "nemotron":  "nemotron-3.5-super-vl",
            "gemma":     "gemma-4-31b",
        })
        self.assertEqual(config.VLLM_MODELS, {"glm", "nemotron", "gemma"})

    def test_openrouter_datierte_kennung(self):
        for name, kennung in config.MODELS.items():
            if name not in config.VLLM_MODELS:
                with self.subTest(name=name):
                    self.assertRegex(kennung, r"-\d{8}$")


class TestClient(unittest.TestCase):

    def lauf(self, model_name, antworten_liste=None, usage=None, choice_extra=None):
        erstellt = []
        self.anfragen = []
        antworten = FakeClient(list(antworten_liste or ABSCHLUSS))
        original = antworten.create

        def create(**kw):
            self.anfragen.append(kw)
            r = original(**kw)
            r.model = "geantwortet/" + kw["model"]
            r.usage = usage
            if choice_extra:
                for k, v in choice_extra.items():
                    setattr(r.choices[0], k, v)
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
        kw, log = self.lauf("sonnet")
        self.assertEqual(kw["base_url"], config.OPENROUTER_BASE_URL)
        self.assertEqual(log["model_string"], "anthropic/claude-sonnet-5.5-20260928")

    def test_sonnet_parameter(self):
        """Effort high, fester Anbieter, Caching auf oberster Ebene, kein temperature/top_p."""
        _, log = self.lauf("sonnet")
        for anfrage in self.anfragen:
            eb = anfrage["extra_body"]
            self.assertEqual(eb["reasoning"], {"effort": "high"})
            self.assertEqual(eb["provider"], {"order": ["anthropic"], "allow_fallbacks": False})
            self.assertEqual(eb["cache_control"], {"type": "ephemeral"})
            self.assertEqual(eb["usage"], {"include": True})
            for verboten in ("temperature", "top_p", "top_k"):
                self.assertNotIn(verboten, anfrage)
                self.assertNotIn(verboten, eb)
        self.assertEqual(log["request_params"]["reasoning"], {"effort": "high"})

    def test_gpt_parameter(self):
        """GPT: Reasoning auf Standard (kein Parameter), kein cache_control (automatisch)."""
        self.lauf("gpt")
        eb = self.anfragen[0]["extra_body"]
        self.assertNotIn("reasoning", eb)
        self.assertNotIn("cache_control", eb)
        self.assertEqual(eb["provider"], {"order": ["openai"], "allow_fallbacks": False})

    def test_nachrichten_unveraendert(self):
        """Caching ändert den Nachrichtenverlauf nicht: kein cache_control in messages."""
        self.lauf("sonnet")
        import json
        for anfrage in self.anfragen:
            self.assertNotIn("cache_control", json.dumps(anfrage["messages"]))

    def test_gateway_ohne_zusatzparameter(self):
        self.lauf("glm")
        self.assertNotIn("extra_body", self.anfragen[0])

    def test_kosten_und_cache_im_log(self):
        usage = SimpleNamespace(total_tokens=110, completion_tokens=10, model_dump=lambda **k: {
            "prompt_tokens": 100, "completion_tokens": 10, "cost": 0.25,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 0}})
        _, log = self.lauf("sonnet", usage=usage)
        self.assertEqual(len(log["finish_reasons"]), 2)
        self.assertAlmostEqual(log["cost_usd"], 0.5)
        self.assertEqual(log["finish_reasons"][0]["cached_tokens"], 80)
        self.assertEqual(log["finish_reasons"][0]["cost"], 0.25)

    def test_verweigerung(self):
        from scoring import run_status, REFUSED
        _, log = self.lauf("sonnet", choice_extra={
            "finish_reason": "stop", "model_extra": {"native_finish_reason": "refusal"}})
        self.assertEqual(log["abort_reason"], "refused")
        self.assertEqual(log["refusal"]["native_finish_reason"], "refusal")
        self.assertEqual(run_status(log), REFUSED)
        self.assertEqual(len(self.anfragen), 1)

    def test_verweigerung_content_filter(self):
        _, log = self.lauf("gpt", choice_extra={"finish_reason": "content_filter"})
        self.assertEqual(log["abort_reason"], "refused")

    def test_antwortendes_modell_im_log(self):
        _, log = self.lauf("nemotron")
        self.assertEqual({f["response_model"] for f in log["finish_reasons"]},
                         {"geantwortet/nemotron-3.5-super-vl"})


if __name__ == "__main__":
    unittest.main()
