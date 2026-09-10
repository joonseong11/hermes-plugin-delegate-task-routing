#!/usr/bin/env bash
set -euo pipefail

PLUGIN_PARENT="${HERMES_HOME:-/opt/data}/plugins"
TARGET="$PLUGIN_PARENT/delegate-task-routing"
APPLY=0
BACKUP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --from) BACKUP="${2:-}"; shift 2 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$BACKUP" ]]; then
  printf 'Usage: %s --from /path/to/.delegate-task-routing.backup-TIMESTAMP [--apply]\n' "$0" >&2
  exit 2
fi
BACKUP="$(readlink -f "$BACKUP")"
case "$BACKUP" in
  "$PLUGIN_PARENT"/.delegate-task-routing.backup-*) ;;
  *) printf 'Refusing backup outside %s or with unexpected name: %s\n' "$PLUGIN_PARENT" "$BACKUP" >&2; exit 2 ;;
esac
[[ -d "$BACKUP" ]] || { printf 'Backup not found: %s\n' "$BACKUP" >&2; exit 2; }

if [[ $APPLY -eq 0 ]]; then
  printf 'Dry run: would restore %s to %s. Pass --apply to continue.\n' "$BACKUP" "$TARGET"
  exit 0
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$PLUGIN_PARENT/.delegate-task-routing.rollback-stage-$$"
FAILED="$PLUGIN_PARENT/.delegate-task-routing.replaced-$STAMP"
cp -a "$BACKUP" "$STAGE"
[[ -e "$TARGET" ]] && mv "$TARGET" "$FAILED"
mv "$STAGE" "$TARGET"
printf 'Rollback restored: %s\n' "$BACKUP"
[[ -d "$FAILED" ]] && printf 'Replaced version retained at: %s\n' "$FAILED"
printf 'Run /restart from Slack before testing the restored plugin.\n'
