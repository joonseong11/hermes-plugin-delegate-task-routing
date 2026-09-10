#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${HERMES_HOME:-/opt/data}/plugins/delegate-task-routing"
REPORT_DIR="${HERMES_HOME:-/opt/data}/artifacts/hermes-routing-update-checks"
PAYLOAD=(
  __init__.py plugin.yaml README.md CHANGELOG.md COMPATIBILITY.md LICENSE
  tests/test_routing.py docs/SLACK_SMOKE_TEST.md
  scripts/verify.sh scripts/deploy.sh scripts/rollback.sh scripts/pre-update-check.sh
  scripts/check-version.sh
)

[[ -d "$TARGET" ]] || { printf 'Installed plugin not found: %s\n' "$TARGET" >&2; exit 2; }
for file in "${PAYLOAD[@]}"; do
  cmp "$ROOT/$file" "$TARGET/$file" || {
    printf 'Source/deployed mismatch: %s\n' "$file" >&2
    exit 3
  }
done

mkdir -p "$REPORT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$REPORT_DIR/pre-update-$STAMP.txt"

{
  printf 'checked_at=%s\n' "$(date -Iseconds)"
  hermes --version
  printf 'source_deployed_comparison=PASS\n\n'
  PLUGIN_PATH="$TARGET/__init__.py" "$ROOT/scripts/verify.sh"
  printf '\nplugin_status:\n'
  hermes plugins list --plain --no-bundled
} | tee "$REPORT"

printf 'Pre-update gate passed against the deployed bytes. Report: %s\n' "$REPORT"
printf 'After updating Hermes: rerun scripts/verify.sh, restart the gateway, and execute docs/SLACK_SMOKE_TEST.md.\n'
