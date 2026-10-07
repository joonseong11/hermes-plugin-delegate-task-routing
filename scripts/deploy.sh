#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_PARENT="${HERMES_HOME:-/opt/data}/plugins"
TARGET="$PLUGIN_PARENT/delegate-task-routing"
APPLY=0
ENABLE=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) ;;
    --apply) APPLY=1 ;;
    --enable) ENABLE=1 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

"$ROOT/scripts/verify.sh"

payload=(
  __init__.py plugin.yaml README.md CHANGELOG.md COMPATIBILITY.md LICENSE
  tests/test_routing.py
  tests/test_five_model_policy.py tests/test_recovery_runtime.py
  docs/SLACK_SMOKE_TEST.md
  scripts/verify.sh scripts/deploy.sh scripts/rollback.sh scripts/pre-update-check.sh
  scripts/check-version.sh
)

printf 'Deployment target: %s\n' "$TARGET"
if [[ $APPLY -eq 0 ]]; then
  printf 'Dry run only. Pass --apply for a live deployment.\n'
  for file in "${payload[@]}"; do printf 'would install %s\n' "$file"; done
  [[ $ENABLE -eq 1 ]] && printf 'would enable plugin and grant tools.override\n'
  exit 0
fi

# This checkout is also the live plugin directory on some installations.
# Moving it would strand its .git metadata in the rollback backup and replace
# the working checkout with a reduced payload. It is already staged in place.
if [[ "$(realpath "$ROOT")" == "$(realpath "$TARGET")" ]]; then
  printf 'Refusing self-deployment of the live plugin checkout; files are already staged in place. Verify, then arrange an external gateway restart.\n' >&2
  exit 2
fi

mkdir -p "$PLUGIN_PARENT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$PLUGIN_PARENT/.delegate-task-routing.stage-$$"
BACKUP="$PLUGIN_PARENT/.delegate-task-routing.backup-$STAMP"
COMMITTED=0

cleanup() {
  status=$?
  rm -rf "$STAGE"
  if [[ $status -ne 0 && $COMMITTED -eq 0 && -d "$BACKUP" ]]; then
    rm -rf "$TARGET"
    mv "$BACKUP" "$TARGET"
    printf 'Deployment failed; previous plugin restored.\n' >&2
  fi
  exit "$status"
}
trap cleanup EXIT

for file in "${payload[@]}"; do
  mode=0644
  [[ "$file" == scripts/*.sh ]] && mode=0755
  install -D -m "$mode" "$ROOT/$file" "$STAGE/$file"
done

if [[ -e "$TARGET" ]]; then
  mv "$TARGET" "$BACKUP"
fi
mv "$STAGE" "$TARGET"

if [[ $ENABLE -eq 1 ]]; then
  hermes plugins enable delegate-task-routing --allow-tool-override
fi

for file in "${payload[@]}"; do
  cmp "$ROOT/$file" "$TARGET/$file"
done
COMMITTED=1
trap - EXIT
printf 'deployment file comparison: PASS\n'
[[ -d "$BACKUP" ]] && printf 'rollback backup: %s\n' "$BACKUP"
printf 'Gateway restart is intentionally not automatic. After an independently reviewed diff, safe session-store snapshot and active-child check, restart from an EXTERNAL host shell; then follow docs/SLACK_SMOKE_TEST.md.\n'
