"""
run_pairs.py
------------
Studienpaare in zufälliger, fester Reihenfolge durch den Harness schicken,
pro Paar alle angegebenen Modelle gleichzeitig (ein run_harness-Prozess je
Modell). Geht das Budget vorzeitig aus, ist die bis dahin gelaufene Auswahl
trotzdem ausgewogen: immer ganze Paare (eine positive, eine negative Studie),
zufällig über die Stichprobe verteilt, für alle Modelle gleich.

Paare wie bisher: i-te Studie aus positiv_40 mit i-ter aus negativ_40 in der
Reihenfolge von subset.csv. Die Reihenfolge der Paare hängt nur vom Seed ab
und wird in logs/paar_reihenfolge_<seed>.json festgehalten.

Resume: run_harness überspringt vorhandene Läufe. Ein Neustart mit demselben
Seed setzt also beim ersten unvollständigen Paar fort.

Beispiele (aus dem Repo-Wurzelverzeichnis):

    python3 harness/run_pairs.py --models sonnet,gpt --reps 1 --dry-run
    python3 harness/run_pairs.py --models sonnet,gpt --reps 1 --max-pairs 1   # Testlauf
    python3 harness/run_pairs.py --models sonnet,gpt --reps 1                 # alle Paare

Nach dem Ende results.csv/summary.json wie gewohnt per Resume über alle
Studien neu erzeugen (Dry-Run auf "0 offen" zuerst): Die parallelen
Prozesse überschreiben results.csv gegenseitig.
"""

import argparse
import json
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import LOGS_DIR, VLLM_MODELS, MODEL_OPENROUTER_KEY, OPENROUTER_KEYS
from run_harness import load_subset, kosten_bisher, budget_fuer

SEED_STANDARD = 20261120
HARNESS = Path(__file__).resolve().parent / "run_harness.py"


def paare(subset: dict) -> list:
    """(positiv, negativ) in der Reihenfolge von subset.csv."""
    pos = [sid for sid, r in subset.items() if r.get("Gruppe") == "positiv_40"]
    neg = [sid for sid, r in subset.items() if r.get("Gruppe") == "negativ_40"]
    if len(pos) != len(neg):
        sys.exit(f"Ungleiche Gruppen: {len(pos)} positiv, {len(neg)} negativ")
    return list(zip(pos, neg))


def reihenfolge(alle: list, seed: int) -> list:
    """Feste Zufallsreihenfolge der Paare; wird protokolliert."""
    gemischt = list(alle)
    random.Random(seed).shuffle(gemischt)
    datei = LOGS_DIR / f"paar_reihenfolge_{seed}.json"
    inhalt = {"seed": seed, "paare": [list(p) for p in gemischt]}
    if datei.exists():
        alt = json.loads(datei.read_text(encoding="utf-8"))
        if alt != inhalt:
            sys.exit(f"{datei} weicht von der berechneten Reihenfolge ab (subset.csv geändert?)")
    else:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        datei.write_text(json.dumps(inhalt, indent=1), encoding="utf-8")
    return gemischt


def befehl(model: str, paar: tuple, args, dry_run: bool) -> list:
    cmd = [sys.executable, "-u", str(HARNESS), "--phase", args.phase,
           "--models", model, "--studies", ",".join(paar),
           "--reps", str(args.reps)]
    if args.budget_usd is not None:
        cmd += ["--budget-usd", str(args.budget_usd)]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def kostenstand(args) -> str:
    """OpenRouter-Kosten je Key, mit Budget, wo eines gilt."""
    teile = []
    for k in OPENROUTER_KEYS:
        grenze = budget_fuer(k, args.budget_usd)
        teile.append(f"{k} {kosten_bisher(k):.2f} USD"
                     + (f" von {grenze:.2f}" if grenze is not None else " (ohne Budget)"))
    return "OpenRouter-Kosten: " + "; ".join(teile)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", required=True, help="kommagetrennt, z. B. sonnet,gpt")
    ap.add_argument("--reps", type=int, required=True)
    ap.add_argument("--phase", choices=["attack", "defense"], default="attack")
    ap.add_argument("--seed", type=int, default=SEED_STANDARD)
    ap.add_argument("--max-pairs", type=int, default=None,
                    help="nur die ersten N Paare der Reihenfolge (z. B. 1 für den Testlauf)")
    ap.add_argument("--parallel-pairs", type=int, default=2,
                    help="so viele Paare gleichzeitig (je Paar ein Prozess pro Modell)")
    ap.add_argument("--budget-usd", type=float, default=None,
                    help="ersetzt das Budget der Keys mit Budget (config.OPENROUTER_KEYS)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    folge = reihenfolge(paare(load_subset()), args.seed)
    if args.max_pairs:
        folge = folge[:args.max_pairs]

    print(f"Seed {args.seed}: {len(folge)} Paare, Modelle {', '.join(models)}, "
          f"Wiederholungen {args.reps}, Phase {args.phase}")
    for k, p in enumerate(folge, 1):
        print(f"  {k:2d}. {p[0]} / {p[1]}")

    if args.dry_run:
        for k, p in enumerate(folge, 1):
            for m in models:
                out = subprocess.run(befehl(m, p, args, True), capture_output=True, text=True)
                plan = [z for z in out.stdout.splitlines() if z.startswith("Plan:")]
                print(f"  Paar {k} {m}: {plan[0] if plan else out.stdout.strip()[-200:]}")
        print()
        print(kostenstand(args))
        print("Dry-Run: nichts ausgeführt.")
        return

    # Keys mit Budget, über die eines der Modelle läuft
    budget_keys = {MODEL_OPENROUTER_KEY[m] for m in models if m not in VLLM_MODELS
                   and budget_fuer(MODEL_OPENROUTER_KEY[m], args.budget_usd) is not None}
    laufend = []          # (paar_nr, model, Popen, logdatei)
    naechstes = 0
    budget_erreicht = False

    while naechstes < len(folge) or laufend:
        # neue Paare starten, solange Platz ist und Budget übrig
        while (naechstes < len(folge) and not budget_erreicht
               and len({nr for nr, *_ in laufend}) < args.parallel_pairs):
            voll = [k for k in budget_keys
                    if kosten_bisher(k) >= budget_fuer(k, args.budget_usd)]
            if voll:
                budget_erreicht = True
                print(f"Budget erreicht (Key {', '.join(voll)}), keine neuen Paare. "
                      f"{kostenstand(args)}")
                break
            p = folge[naechstes]
            naechstes += 1
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            for m in models:
                logdatei = LOGS_DIR / f"run_{ts}_{m}_paar{naechstes:02d}.txt"
                f = open(logdatei, "w", encoding="utf-8")
                proc = subprocess.Popen(befehl(m, p, args, False), stdout=f,
                                        stderr=subprocess.STDOUT)
                laufend.append((naechstes, m, proc, f))
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] Paar {naechstes}/{len(folge)} "
                  f"gestartet: {p[0]} / {p[1]}", flush=True)

        if budget_erreicht and naechstes < len(folge) and not laufend:
            break
        if naechstes >= len(folge) and not laufend:
            break

        time.sleep(15)
        for eintrag in list(laufend):
            nr, m, proc, f = eintrag
            if proc.poll() is not None:
                f.close()
                laufend.remove(eintrag)
                print(f"[{datetime.now():%Y-%m-%d %H:%M}] Paar {nr} {m} fertig "
                      f"(Exit {proc.returncode}); {kostenstand(args)}",
                      flush=True)

    print(f"\nEnde. Gestartete Paare: {naechstes} von {len(folge)}. "
          f"{kostenstand(args)}")


if __name__ == "__main__":
    main()
