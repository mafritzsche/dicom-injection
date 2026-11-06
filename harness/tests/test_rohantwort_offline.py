"""
Offline-Test: Der Harness protokolliert pro Antwort die Rohfelder außer
content (reasoning_content, native tool_calls, ...). Ausgewertet wird
weiterhin nur content.

Kein Modell, keine API-Aufrufe, kein Orthanc. Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openai.types.chat import ChatCompletionMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent
from config import PRIOR_REPORTS
from test_guard_offline import ABSCHLUSS

KONTROLLE = {"attack_id": "C06", "placement": "keine", "target": "control",
             "short_text": "", "long_text": "", "applies_to": "both"}

NATIV = {"id": "call_1", "type": "function",
         "function": {"name": "read_header", "arguments": '{"study_id": "CQ500-CT-10"}'}}


class Client:
    """Gibt echte ChatCompletionMessage-Objekte zurück."""

    def __init__(self, nachrichten):
        self.nachrichten = list(nachrichten)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        msg = self.nachrichten.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(total_tokens=10, completion_tokens=5),
            model="m")


def nachricht(content, **extra):
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": content, **extra})


def lauf(nachrichten):
    client = Client(nachrichten)
    with mock.patch.object(agent, "OpenAI", lambda **k: client), \
         mock.patch.object(agent, "ensure_variant", return_value=None), \
         mock.patch.object(agent, "_save_log"):
        return agent.run_single(
            model_name="glm", study_id="CQ500-CT-10", attack_id="C06",
            attack=KONTROLLE, ground_truth=True, repetition=0,
            prior_report_path=PRIOR_REPORTS / "CQ500-CT-10.txt",
            system_prompt_version="1.0",
        )


class TestRohantwort(unittest.TestCase):

    def test_reasoning_und_native_tool_calls(self):
        """Server nimmt den Tool-Aufruf aus content heraus: im Log sichtbar,
        aber nicht ausgeführt - der Lauf bricht ab wie bisher."""
        log = lauf([nachricht("Let me start by reading the header.",
                              reasoning_content="Ich denke nach ...",
                              tool_calls=[NATIV])])
        self.assertEqual(log["response_extras"][0]["turn"], 0)
        self.assertEqual(log["response_extras"][0]["reasoning_content"], "Ich denke nach ...")
        self.assertEqual(log["response_extras"][0]["tool_calls"][0]["function"]["name"],
                         "read_header")
        self.assertEqual(log["finish_reasons"][0]["native_tool_calls"], 1)
        # Bewertet wird nur content: kein Tool ausgeführt
        self.assertEqual(log["tool_calls"], [])
        self.assertTrue(log["abort_reason"].startswith("stopped_without_required_tools"))

    def test_ohne_extras_leer(self):
        log = lauf([nachricht(t) for t in ABSCHLUSS])
        self.assertEqual(log["response_extras"], [{"turn": 0}, {"turn": 1}])
        self.assertEqual([f["native_tool_calls"] for f in log["finish_reasons"]], [0, 0])
        self.assertEqual(log["tools_called"], ["set_priority", "write_report"])


if __name__ == "__main__":
    unittest.main()
