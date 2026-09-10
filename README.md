# delegate-task-routing

Current release: `v0.2.8`. See [COMPATIBILITY.md](COMPATIBILITY.md) before changing the Hermes version and [CHANGELOG.md](CHANGELOG.md) for release history.

Persistent, in-process orchestration policy for Hermes. It forces a per-turn execution decision on Slack parent sessions, applies exact-allowlisted per-task routing, records requested versus actual child execution, and deterministically prepends the `_Alex: ..._` execution header.

## Per-turn policy

Every normal Slack parent turn must call `route_turn` first. LLM request middleware forces that exact tool choice until a valid decision exists:

- `direct`: parent handles the request; no delegation lanes.
- `single`: exactly one worker lane.
- `parallel`: at least two worker lanes.
- `worker_verifier`: at least one worker and one verifier lane.

After a delegated decision, the next model call is forced to `delegate_task`. A `pre_tool_call` gate blocks unrelated tools between the decision and dispatch. Subagents and non-Slack runtimes are excluded from the parent-turn gate and from Alex headers.

## Added task fields

```json
{
  "goal": "Analyze the operational risk",
  "label": "위험분석",
  "model": "gpt-5.6-terra-900k",
  "reasoning_effort": "high",
  "toolsets": ["file", "code_execution"]
}
```

`goal`, `label`, `model`, `reasoning_effort`, and `toolsets` are mandatory. Omission fails before child construction. The dispatched tasks must exactly match the preceding `route_turn` lanes.

## Deterministic response header

`transform_llm_output` removes any model-written Alex header and prepends one generated from policy state. Direct work renders `_Alex: Sol-medium · 직접 처리_`. Delegated completion resolves `async_delegations.event_json` from the Hermes state database and uses actual result metadata. A model or effort fallback renders explicitly, for example `_Alex: Sol-medium · 자료수집: Luna-low → Terra-unknown_`.

The header preserves owned terminal async outcomes (`completed`, `error`, and
restart-recovered `unknown`) so failed/timeout lanes render as failures rather
than generic missing information. If a record is genuinely missing, pruned, or
cannot be authenticated to the parent session, it is labeled `위임 기록 만료 · 실행경로 미검증`; attribution is never invented.

`worker_verifier` is sequenced across two durable async delegations. The first dispatch contains worker lanes only. Its completion event creates a new policy turn that forces the declared verifier lanes, injects the worker result into that turn, and prevents final-completion reporting until the verifier delegation returns. The worker and verifier route records are then merged for the final header. The continuation record is stored through the profile-scoped plugin-state API so a gateway restart between stages does not silently collapse the plan.

## Safety boundaries

- Models, effort levels, and toolsets use normalized membership checks against allowlists under `plugins.entries.delegate-task-routing.settings`.
- When a task explicitly names toolsets and the parent exposes a concrete enabled-toolset list, the task cannot request a broader named set; the constructed child is checked again before launch.
- Provider, endpoint, credentials, ACP transport, and API mode cannot be selected by the model.
- `ultra` is not accepted as reasoning effort; parallel lanes are the orchestration mechanism.
- Routing state uses `contextvars`, so concurrent gateway sessions do not share task choices.
- Child results include requested values and locally observed child model, provider, effort, toolsets, and session ID. These fields are not remote-provider attestation.
- Routed operation is supported for the active single-profile deployment. The plugin patches process-global private Python functions and is not validated for different allowlists across multiple profiles in one gateway process.

## Update behavior

The plugin lives under `/opt/data/plugins`, not the replaceable `/opt/hermes` image, so its files survive normal container/image replacement when `/opt/data` remains persistently mounted. Behavioral compatibility with a later Hermes version is not implied.

At load time it verifies selected native function names and signatures. If those structural checks fail, the plugin fails closed instead of guessing. Because routing also depends on Hermes constructing children synchronously before background dispatch, upgrades still require behavioral smoke tests. If a future Hermes release exposes all four field names (`label`, `model`, `reasoning_effort`, `toolsets`) natively, the plugin does not patch the built-in implementation; operators must separately verify that the native policy semantics match this plugin.

Allowlist settings and the extended schema are captured when the plugin loads. Changing them requires a fresh CLI session or gateway restart.

## Verification

```bash
PYTHONPATH=/opt/hermes uv run --no-project --with pytest python -m pytest \
  /opt/data/plugins/delegate-task-routing/tests/test_routing.py -q \
  -k 'not installed_hermes_contract'

/opt/hermes/.venv/bin/python - <<'PY'
import importlib.util
p = '/opt/data/plugins/delegate-task-routing/__init__.py'
s = importlib.util.spec_from_file_location('routing_contract', p)
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
import tools.delegate_tool as live
m._assert_compatible(live)
print('installed contract: PASS')
PY
```

## Disable / rollback

```bash
hermes plugins disable delegate-task-routing
```

A gateway restart or fresh CLI session is required after enable/disable. Disabling restores Hermes' built-in `delegate_task`; existing completed session records are not changed.

## Repository workflow

Fresh installation from a trusted checkout:

```bash
./scripts/verify.sh
./scripts/deploy.sh
./scripts/deploy.sh --apply --enable
```

The first deploy command is a dry run. `--apply` is mandatory for a write, and `--enable` uses Hermes' official CLI to enable the plugin and grant its declared `tools.override` capability. Unknown arguments fail without deploying. An existing installation that is already enabled may use `./scripts/deploy.sh --apply`.

Deployment stages a complete replacement, retains a timestamped backup, and verifies every installed byte. It intentionally does not restart the gateway. After deployment, run `/restart` from Slack and complete [docs/SLACK_SMOKE_TEST.md](docs/SLACK_SMOKE_TEST.md).

Rollback is also dry-run by default:

```bash
./scripts/rollback.sh --from /opt/data/plugins/.delegate-task-routing.backup-TIMESTAMP
./scripts/rollback.sh --from /opt/data/plugins/.delegate-task-routing.backup-TIMESTAMP --apply
```

Before a Hermes update:

```bash
./scripts/pre-update-check.sh
```

The generated report records the currently working Hermes/plugin pair. After the update, rerun verification and the Slack post-restart smoke test before declaring compatibility.

## Release policy

- `main` must pass the pinned supported-Hermes CI job.
- Latest Hermes `main` is tested as an experimental, non-blocking compatibility signal.
- Release tags use `vMAJOR.MINOR.PATCH`.
- Never bypass a failed private-symbol compatibility check.
- A passing unit suite is not a substitute for the post-restart Slack test.

## Verification boundary

`scripts/verify.sh` proves compilation, the complete automated policy suite, and compatibility with the installed Hermes delegation symbols. `scripts/pre-update-check.sh` additionally requires the tested source and deployed plugin bytes to match. CI repeats the suite against the pinned supported Hermes commit and treats current Hermes `main` as experimental.

These automated checks do **not** prove a real provider request, gateway restart continuity, or Slack delivery. Those remain explicit release gates in [docs/SLACK_SMOKE_TEST.md](docs/SLACK_SMOKE_TEST.md); do not describe a release as fully operational until that checklist passes.
