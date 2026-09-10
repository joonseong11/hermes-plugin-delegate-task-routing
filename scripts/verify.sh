#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_SRC="${HERMES_SRC:-/opt/hermes}"
HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
PLUGIN_UNDER_TEST="${PLUGIN_PATH:-$ROOT/__init__.py}"

if [[ ! -f "$HERMES_SRC/tools/delegate_tool.py" ]]; then
  printf 'Hermes source not found: %s\n' "$HERMES_SRC" >&2
  exit 2
fi
if [[ ! -x "$HERMES_PYTHON" ]]; then
  printf 'Hermes Python not found: %s\n' "$HERMES_PYTHON" >&2
  exit 2
fi

python3 -m py_compile "$PLUGIN_UNDER_TEST" "$ROOT/tests/test_routing.py"
PYTHONPATH="$HERMES_SRC:${HERMES_SRC}/.venv/lib/python3.13/site-packages${PYTHONPATH:+:$PYTHONPATH}" \
  PLUGIN_PATH="$PLUGIN_UNDER_TEST" uv run --no-project --with pytest python -m pytest -q "$ROOT/tests/test_routing.py"

PYTHONPATH="$HERMES_SRC:${HERMES_SRC}/.venv/lib/python3.13/site-packages${PYTHONPATH:+:$PYTHONPATH}" \
PLUGIN_PATH="$PLUGIN_UNDER_TEST" "$HERMES_PYTHON" - <<'PY'
import importlib.util
import os

path = os.environ["PLUGIN_PATH"]
spec = importlib.util.spec_from_file_location("delegate_task_routing_contract", path)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
import tools.delegate_tool as delegate_module
plugin._assert_compatible(delegate_module)
print("installed Hermes contract: PASS")
PY

printf 'verification: PASS\n'
