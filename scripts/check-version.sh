#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$ROOT" python3 - <<'PY'
from pathlib import Path
import os, re
root = Path(os.environ["ROOT"])
code = (root / "__init__.py").read_text()
manifest = (root / "plugin.yaml").read_text()
readme = (root / "README.md").read_text()
changelog = (root / "CHANGELOG.md").read_text()

def one(pattern, text, label):
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise SystemExit(f"missing {label}")
    return match.group(1)

def changelog_version(text):
    # `## [Unreleased]` may sit on top while a release branch is open; main and tags must not have it.
    heads = re.findall(r'^## \[[^]]+\].*$', text, re.MULTILINE)
    if heads and heads[0].lower().startswith("## [unreleased]"):
        if os.environ.get("GITHUB_REF_TYPE", "") == "tag" or os.environ.get("GITHUB_REF_NAME", "") == "main":
            raise SystemExit("CHANGELOG.md still has an Unreleased section on main or on a tag")
        heads = heads[1:]
    if not heads:
        raise SystemExit("missing CHANGELOG release")
    match = re.fullmatch(r'## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}', heads[0].rstrip())
    if not match:
        raise SystemExit(f"topmost CHANGELOG release heading is not `## [X.Y.Z] - YYYY-MM-DD`: {heads[0]}")
    return match.group(1)

values = {
    "code": one(r'^PLUGIN_VERSION = "([^"]+)"$', code, "PLUGIN_VERSION"),
    "manifest": one(r'^version:\s*([^\s]+)$', manifest, "manifest version"),
    "readme": one(r'^Current release: `v([^`]+)`', readme, "README release"),
    "changelog": changelog_version(changelog),
}
if len(set(values.values())) != 1:
    raise SystemExit(f"version mismatch: {values}")
version = next(iter(values.values()))
if not re.fullmatch(r'\d+\.\d+\.\d+', version):
    raise SystemExit(f"version is not X.Y.Z: {version}")
ref_type = os.environ.get("GITHUB_REF_TYPE", "")
ref_name = os.environ.get("GITHUB_REF_NAME", "")
if ref_type == "tag" and ref_name != f"v{version}":
    raise SystemExit(f"tag/version mismatch: tag={ref_name}, version={version}")
print(f"version consistency: PASS ({version})")
PY
