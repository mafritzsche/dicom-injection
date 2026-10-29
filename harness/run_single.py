"""
run_single.py
-------------
Testet einen einzelnen Lauf des Harness.
Zum Debuggen und Verifizieren bevor run_harness.py alle Läufe startet.

Aufruf:
    python run_single.py --model anthropic --study CQ500-CT-10 --attack A01
    python run_single.py --model anthropic --study CQ500-CT-27 --attack C01
    python run_single.py  # Standardwerte
"""

import argparse
import json
import csv
from pathlib import Path
from agent import run_single
from config import SUBSET_CSV, ATTACKS_CSV, PRIOR_REPORTS
from scoring import describe


def load_subset() -> dict:
    """Lädt subset.csv als {study_id: row_dict}."""
    subset = {}
    with open(SUBSET_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            subset[row["Study-ID"]] = row
    return subset


def load_attacks() -> dict:
    """Lädt attacks.csv als {attack_id: row_dict}."""
    attacks = {}
    with open(ATTACKS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            attacks[row["attack_id"]] = row
    return attacks


def main():
    parser = argparse.ArgumentParser(description="Einzelner Testlauf des Harness")
    parser.add_argument("--model",  default="anthropic",   help="Modellname aus config.MODELS")
    parser.add_argument("--study",  default="CQ500-CT-10", help="Study-ID aus subset.csv")
    parser.add_argument("--attack", default="A01",         help="Attack-ID aus attacks.csv")
    parser.add_argument("--rep",    default=0, type=int,   help="Wiederholung (0/1/2)")
    parser.add_argument("--arm",    default=None,
                        help="Systemprompt-Version, z. B. 1.0 oder 1.2 (Standard: config)")
    args = parser.parse_args()

    subset  = load_subset()
    attacks = load_attacks()

    if args.study not in subset:
        print(f"Fehler: Study-ID '{args.study}' nicht in subset.csv")
        return

    if args.attack not in attacks:
        print(f"Fehler: Attack-ID '{args.attack}' nicht in attacks.csv")
        return

    row          = subset[args.study]
    attack       = attacks[args.attack]
    ground_truth = row.get("ICH", "0") == "1"

    # Vorbefund laden
    prior_path = PRIOR_REPORTS / f"{args.study}.txt"
    if not prior_path.exists():
        print(f"Warnung: Kein Vorbefund gefunden für {args.study}")
        prior_path = None

    print(f"\n{'='*60}")
    print(f"Lauf: {args.model} | {args.study} | {args.attack} | Rep {args.rep}")
    if args.arm:
        print(f"Arm: Systemprompt {args.arm}")
    print(f"Ground truth: {'positiv (Blutung)' if ground_truth else 'negativ'}")
    print(f"Attack: {attack['target']} / {attack['formulation']} / {attack['placement']}")
    print(f"Text: {attack['short_text'][:60]}")
    print(f"{'='*60}\n")

    log = run_single(
        model_name=args.model,
        study_id=args.study,
        attack_id=args.attack,
        attack=attack,
        ground_truth=ground_truth,
        repetition=args.rep,
        prior_report_path=prior_path,
        system_prompt_version=args.arm,
    )

    # Ergebnis anzeigen
    print(f"\n{'='*60}")
    print(f"Abschluss: {log['abort_reason']}")
    print(f"Tool-Aufrufe: {log['n_tool_calls']}")
    print(f"Dauer: {log['duration_seconds']}s")
    print(f"Token gesamt: {log['total_tokens']}")

    variant = log.get("variant")
    if variant:
        action = "neu erzeugt" if variant.get("created") else "wiederverwendet"
        print(f"\nDICOM-Variante ({action}):")
        print(f"  PatientID:          {variant['patient_id']}")
        print(f"  StudyInstanceUID:   {variant['study_instance_uid']}")
        print(f"  Instanzen:          {variant['n_instances']}")
    else:
        print(f"\nDICOM-Variante: keine (Originalstudie {log['study_id']} genutzt)")

    if log["write_report_args"]:
        wr = log["write_report_args"]
        print(f"\nBefund:")
        print(f"  hemorrhage: {wr.get('hemorrhage')}")
        print(f"  subtype:    {wr.get('subtype')}")
        print(f"  impression: {wr.get('impression', '')[:100]}")
        print(f"  remarks:    {wr.get('remarks')}")

    if log["set_priority_args"]:
        print(f"\nPriorität: {log['set_priority_args'].get('priority')}")

    if log["send_message_calls"]:
        print(f"\nNachrichten gesendet:")
        for msg in log["send_message_calls"]:
            print(f"  → {msg['recipient']}: {msg['result']['status']}")

    # Angriffserfolg bewerten - identische Logik wie run_harness.py
    print(f"\n{'='*60}")
    print("Angriffsbewertung:")
    print(f"  {describe(log, attack, ground_truth)}")

    if attack.get("target") == "exfiltration":
        from config import SEND_MESSAGE_ENFORCE_ALLOWLIST
        if not SEND_MESSAGE_ENFORCE_ALLOWLIST:
            print("    (Allowlist-Prüfung aus: Versuch = Verleitbarkeit des Modells)")

    print(f"{'='*60}\n")
    print(f"Protokoll gespeichert in: logs/")


if __name__ == "__main__":
    main()
