"""
Offline-Test für den Neustart (Resume) und das atomare Schreiben der Logs.

Ein Plan wird mitten im Lauf abgebrochen (KeyboardInterrupt, wie Strg+C oder
ein abgestürztes tmux) und dann neu gestartet. Geprüft wird: Erledigte Läufe
werden nicht noch einmal ausgeführt, der abgebrochene und ein Lauf mit
API-Fehler werden wiederholt, am Ende hat jeder Lauf genau ein gültiges Log
und results.csv enthält jeden Lauf genau einmal.

Kein Modell, keine API-Aufrufe, kein Orthanc; Logs in einem temporären
Ordner. Aufruf aus harness/:

    python -m unittest discover -s tests -v
"""

import csv
import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent
import run_harness

KONTROLLE = {"attack_id": "C06", "placement": "keine", "target": "control",
             "short_text": "", "long_text": "", "applies_to": "both"}
ARGS = SimpleNamespace(no_resume=False, verbose=False, allow_prompt_only_arms=False,
                       budget_usd=300.0)


def plan() -> list:
    """3 Studien x 2 Wiederholungen = 6 Läufe."""
    return [
        {"arm": "1.0", "model": "sonnet", "study_id": sid, "attack_id": "C06",
         "rep": rep, "ground_truth": False, "attack": KONTROLLE}
        for sid in ("CQ500-CT-1", "CQ500-CT-2", "CQ500-CT-3")
        for rep in range(2)
    ]


def schluessel(study_id, rep):
    return run_harness.run_key(study_id, "C06", "sonnet", "1.0", rep)


class FakeRunSingle:
    """
    Ersatz für agent.run_single: schreibt ein minimales Log über den echten
    agent._save_log. Startzeiten steigen je Aufruf um eine Minute, damit
    Wiederholungen eigene Dateinamen bekommen wie im echten Betrieb. Die Uhr
    läuft über alle Starts weiter: Eine Wiederholung ist immer später als
    ihr Vorgänger, und run_harness._log_path nimmt die neueste Datei.
    """

    zeit = datetime(2026, 11, 5, tzinfo=timezone.utc)

    def __init__(self, abbruch_bei=None, api_fehler=()):
        self.aufrufe = Counter()
        self.n = 0
        self.abbruch_bei = abbruch_bei      # n-ter Aufruf bricht ab, ohne Log
        self.api_fehler = set(api_fehler)   # Schlüssel mit API-Fehler (einmalig)

    def __call__(self, model_name, study_id, attack_id, attack, ground_truth,
                 repetition, prior_report_path, system_prompt_version):
        self.n += 1
        key = schluessel(study_id, repetition)
        self.aufrufe[key] += 1
        if self.n == self.abbruch_bei:
            raise KeyboardInterrupt
        FakeRunSingle.zeit += timedelta(minutes=1)
        log = {
            "study_id": study_id, "attack_id": attack_id, "model": model_name,
            "system_prompt_version": system_prompt_version, "arm": system_prompt_version,
            "repetition": repetition, "ground_truth": ground_truth,
            "started_at": FakeRunSingle.zeit.isoformat(), "invalid": False,
            "abort_reason": None, "tool_calls": [], "tools_called": [],
        }
        log["cost_usd"] = 1.5
        if key in self.api_fehler:
            self.api_fehler.discard(key)
            log["abort_reason"] = "API error: Timeout"
        agent._save_log(log)
        return log


class TestResume(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)
        self.patches = [mock.patch.object(agent, "LOGS_DIR", self.logs),
                        mock.patch.object(run_harness, "LOGS_DIR", self.logs)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def ausfuehren(self, fake):
        with mock.patch.object(run_harness, "run_single", fake):
            return run_harness.execute(plan(), ARGS, run_harness.existing_runs())

    def test_abbruch_und_neustart(self):
        api = schluessel("CQ500-CT-1", 1)        # 2. Lauf: API-Fehler

        # Erster Start: Läufe 1-3 laufen durch (Lauf 2 mit API-Fehler),
        # beim 4. Lauf wird abgebrochen
        erster = FakeRunSingle(abbruch_bei=4, api_fehler=[api])
        with self.assertRaises(KeyboardInterrupt):
            self.ausfuehren(erster)
        self.assertEqual(len(list(self.logs.glob("*.json"))), 3)

        # Neustart
        zweiter = FakeRunSingle()
        records = self.ausfuehren(zweiter)

        erledigt = {schluessel("CQ500-CT-1", 0), schluessel("CQ500-CT-2", 0)}
        abgebrochen = schluessel("CQ500-CT-2", 1)
        # Erfolgreich erledigte Läufe werden nicht erneut ausgeführt
        for key in erledigt:
            self.assertEqual(zweiter.aufrufe[key], 0, key)
        # API-Fehler und abgebrochener Lauf werden wiederholt, der Rest
        # ausgeführt - jeweils genau einmal
        self.assertEqual(zweiter.aufrufe[api], 1)
        self.assertEqual(zweiter.aufrufe[abgebrochen], 1)
        self.assertEqual(sum(zweiter.aufrufe.values()), 4)

        # Jeder Lauf hat genau ein gültiges Log
        gueltig = Counter()
        for pfad in self.logs.glob("*.json"):
            d = json.loads(pfad.read_text(encoding="utf-8"))
            if not str(d.get("abort_reason") or "").startswith("API error"):
                gueltig[schluessel(d["study_id"], d["repetition"])] += 1
        self.assertEqual(set(gueltig), {schluessel(p["study_id"], p["rep"]) for p in plan()})
        self.assertEqual(set(gueltig.values()), {1})
        # Das Fehler-Log bleibt zur Dokumentation erhalten
        self.assertEqual(len(list(self.logs.glob("*.json"))), 7)

        # results.csv: jeder Lauf genau einmal, Fehler-Log nicht verwendet
        pfad = run_harness.write_results(records)
        with open(pfad, newline="", encoding="utf-8") as f:
            zeilen = list(csv.DictReader(f))
        keys = [schluessel(z["study_id"], int(z["rep"])) for z in zeilen]
        self.assertEqual(len(keys), 6)
        self.assertEqual(len(set(keys)), 6)
        self.assertEqual(sum(z["resumed"] == "True" for z in zeilen), 2)
        for z in zeilen:
            d = json.loads(Path(z["log_path"]).read_text(encoding="utf-8"))
            self.assertIsNone(d["abort_reason"])

        # Dritter Start: nichts mehr offen
        dritter = FakeRunSingle()
        self.ausfuehren(dritter)
        self.assertEqual(sum(dritter.aufrufe.values()), 0)

    def test_kosten_werden_gebucht(self):
        self.ausfuehren(FakeRunSingle())
        self.assertAlmostEqual(run_harness.kosten_bisher(), 6 * 1.5)
        self.assertAlmostEqual(run_harness.kosten_bisher("sonnet"), 6 * 1.5)
        self.assertEqual(run_harness.kosten_bisher("gpt_google"), 0.0)
        self.assertTrue((self.logs / "kosten_openrouter.jsonl").exists())

    def test_kostenbuch_je_key(self):
        """GPT bucht in das Kostenbuch des Keys ohne Limit."""
        fake = FakeRunSingle()
        laeufe = [{**p, "model": "gpt"} for p in plan()]
        with mock.patch.object(run_harness, "run_single", fake):
            run_harness.execute(laeufe, ARGS, {})
        self.assertAlmostEqual(run_harness.kosten_bisher("gpt_google"), 6 * 1.5)
        self.assertEqual(run_harness.kosten_bisher("sonnet"), 0.0)
        zeile = json.loads((self.logs / "kosten_openrouter_gpt_google.jsonl")
                           .read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(zeile["openrouter_key"], "gpt_google")

    def test_gpt_ohne_budget_stopp(self):
        """Ist das Budget des Sonnet-Keys erreicht, läuft GPT trotzdem weiter."""
        self.ausfuehren(FakeRunSingle())                      # 9 USD auf sonnet
        budget = SimpleNamespace(**{**vars(ARGS), "budget_usd": 1.0})
        fake = FakeRunSingle()
        laeufe = [{**p, "model": "gpt"} for p in plan()]
        with mock.patch.object(run_harness, "run_single", fake):
            run_harness.execute(laeufe, budget, {})
        self.assertEqual(fake.n, 6)
        self.assertIsNone(run_harness.budget_fuer("gpt_google", 1.0))
        self.assertEqual(run_harness.budget_fuer("sonnet"), 300.0)
        self.assertEqual(run_harness.budget_fuer("sonnet", 1.0), 1.0)

    def test_budget_stoppt_neue_laeufe(self):
        """Nach Erreichen des Budgets startet kein neuer Lauf; Resume setzt später fort."""
        fake = FakeRunSingle()
        budget = SimpleNamespace(**{**vars(ARGS), "budget_usd": 3.0})
        with mock.patch.object(run_harness, "run_single", fake):
            records = run_harness.execute(plan(), budget, run_harness.existing_runs())
        self.assertEqual(fake.n, 2)             # 2 x 1,5 USD = Budget erreicht
        self.assertEqual(len(records), 2)
        fake2 = FakeRunSingle()
        records = self.ausfuehren(fake2)        # Budget 300: Rest läuft
        self.assertEqual(fake2.n, 4)
        self.assertEqual(len(records), 6)

    def test_gateway_ohne_budget(self):
        """Läufe über das Gateway kosten nichts und werden nicht gebremst."""
        fake = FakeRunSingle()
        budget = SimpleNamespace(**{**vars(ARGS), "budget_usd": 0.0})
        laeufe = [{**p, "model": "glm"} for p in plan()]
        with mock.patch.object(run_harness, "run_single", fake):
            run_harness.execute(laeufe, budget, {})
        self.assertEqual(fake.n, 6)
        self.assertEqual(list(self.logs.glob("kosten*.jsonl")), [])

    def test_halbe_datei_bleibt_nicht_liegen(self):
        """Abbruch mitten im Schreiben: weder Log noch Temporärdatei bleiben."""
        log = {"study_id": "CQ500-CT-1", "attack_id": "C06", "model": "sonnet",
               "system_prompt_version": "1.0", "repetition": 0,
               "started_at": "2026-11-05T10:00:00+00:00"}

        def kaputt(*a, **k):
            a[1].write('{"study_id": "CQ5')
            raise KeyboardInterrupt

        with mock.patch.object(agent.json, "dump", kaputt), \
             self.assertRaises(KeyboardInterrupt):
            agent._save_log(log)
        self.assertEqual(list(self.logs.iterdir()), [])
        self.assertEqual(run_harness.existing_runs(), {})

        agent._save_log(log)
        self.assertEqual(len(run_harness.existing_runs()), 1)


if __name__ == "__main__":
    unittest.main()
