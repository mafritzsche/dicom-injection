#!/usr/bin/env bash
# Sichert die Studien-Logs auf das MRI-Netzlaufwerk (U:\dicom-injection\Logs).
#
# Kopiert aus logs/ (nur oberste Ebene, keine Pilot-Unterordner):
#   *.json          Lauf-Logs (+ summary.json)
#   results.csv
#   run_*.txt       Mitschnitte der tmux-Sitzungen
# Gleichnamige Dateien auf U: werden überschrieben, auf dem Server gelöschte
# Dateien bleiben auf U: erhalten. Jeder Durchlauf schreibt eine Zeile nach
# logs/backup.log.
#
# Anmeldung über ~/.smbcredentials (username/password/domain, chmod 600).
# Stündlich per Cron:
#   0 * * * * /data/fritzsche/dicom-injection/harness/backup_logs.sh

set -u

LOGS="/data/fritzsche/dicom-injection/logs"
SHARE="//mri-p-u-evs.mri.tu-muenchen.de/USER\$"
ZIEL="v0363/dicom-injection/Logs"
CRED="$HOME/.smbcredentials"
PROTOKOLL="$LOGS/backup.log"

mkdir -p "$LOGS"
jetzt() { date '+%Y-%m-%d %H:%M:%S'; }

# Kein zweiter Durchlauf, solange einer läuft (große Kopie > 1 h)
exec 9>"$LOGS/.backup.lock"
if ! flock -n 9; then
    echo "$(jetzt) übersprungen: vorheriges Backup läuft noch" >> "$PROTOKOLL"
    exit 0
fi

anzahl=$(find "$LOGS" -maxdepth 1 -type f \
         \( -name '*.json' -o -name 'results.csv' -o -name 'run_*.txt' \) | wc -l)

ausgabe=$(smbclient "$SHARE" -A "$CRED" -D "$ZIEL" \
          -c "prompt OFF; lcd $LOGS; mput *.json; mput results.csv; mput run_*.txt" 2>&1)
status=$?

# smbclient meldet manche Fehler nur im Text
if [ $status -eq 0 ] && ! grep -q "NT_STATUS_" <<< "$ausgabe"; then
    echo "$(jetzt) ok: $anzahl Dateien" >> "$PROTOKOLL"
else
    fehler=$(grep -m1 "NT_STATUS_" <<< "$ausgabe")
    echo "$(jetzt) FEHLER (Exit $status): ${fehler:-$(tail -n1 <<< "$ausgabe")}" >> "$PROTOKOLL"
    exit 1
fi
