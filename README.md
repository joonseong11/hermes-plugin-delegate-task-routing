# delegate-task-routing

Current release: `v0.5.0` (gateway activation pending). See [COMPATIBILITY.md](COMPATIBILITY.md) before changing the Hermes version and [CHANGELOG.md](CHANGELOG.md) for release history.

Persistent, in-process orchestration policy for Hermes. It forces a per-turn execution decision on Slack parent sessions, applies exact-allowlisted per-task routing, records requested versus actual child execution, and deterministically prepends the `_Alex: ..._` execution header.

## Per-turn policy

### Localized Figma edits and refreshed skills

Clear icon/color/copy/alignment/spacing/visibility edits use direct execution when they do not change shared component definitions, Variant/Property schemas, navigation contracts, or permissions. The parent reloads user-requested changed skills, preserves user changes, records each external write, and reads back and visually checks every changed screen. Repeating the same local change on several screens is not itself a reason to delegate.

Routing remains the first tool call. A requested skill refresh selects direct with empty lanes before skill_view. If that read reveals a larger scope, the parent reroutes before any write. Already-delegated work and completion turns retain their existing routing; this is not a general route-reset permission.

This is model-facing classification guidance, not an automatic Figma risk classifier. Its effect must be checked in actual task traces. Changing these Python files does not hot-reload an already imported plugin; no gateway restart is performed as part of this file-only change.

Every normal Slack parent turn must call `route_turn` first. LLM request middleware forces that exact tool choice until a valid decision exists:

- `direct`: trivial lookup, brief judgment, bounded read-only checks, or small reversible local edits; no delegation lanes.
- `single`: substantial general research/implementation in exactly one worker lane, including its own focused tests. Parent reviews evidence.
- `parallel`: at least two genuinely independent outcomes. Implementation and its dependent tests are not separate parallel outcomes.

### No automatic verification (v0.5.0)

The plugin never adds a verification or review stage on its own. Earlier versions forced a second verifier delegation when the request wording matched high-risk keywords (`worker_verifier`), and opened a repository-fingerprint review gate for requests that looked like development work (`development_review`). Both were removed in v0.5.0, together with the keyword classifiers that triggered them.

Verification is now requested by the person who needs it. When a user asks for an independent check or review, the parent routes it as an ordinary lane with `work_type=verification`, which still uses the fixed verification model. Nothing blocks or annotates a final answer for lack of a review.

A review can only cover work that already exists. The plugin has no sequential mode, and a completion turn cannot dispatch further lanes, so work and a review of that work cannot run from one message: the guidance tells the parent to delegate the work only and to say that the review has not run. The user asks for the review in a follow-up message once the work is back.

Records written by earlier versions are handled as follows. A delegation left in a `workers_dispatched` or `verifiers_dispatched` stage is accepted once as an ordinary completion; no verifier is dispatched for it. The `development_reviews` plugin-state key is no longer read or written.

After a delegated decision, the next model call is forced to `delegate_task`. A `pre_tool_call` gate blocks unrelated tools between the decision and dispatch. Subagents and non-Slack runtimes are excluded from the parent-turn gate and from Alex headers.

## Added task fields

```json
{
  "goal": "Analyze the operational risk",
  "label": "위험분석",
  "model": "claude-opus-5",
  "reasoning_effort": "high",
  "toolsets": ["file", "code_execution"]
}
```

`goal`, `label`, `model`, `reasoning_effort`, and `toolsets` are mandatory. Omission fails before child construction. The dispatched tasks must exactly match the preceding `route_turn` lanes.

## Deterministic response header

`transform_llm_output` removes any model-written Alex header and prepends one generated from policy state. Direct work renders `_Alex: Sol-medium · 직접 처리_`. Delegated completion resolves `async_delegations.event_json` from the Hermes state database and uses actual result metadata. A model or effort fallback renders explicitly, for example `_Alex: Sol-medium · 자료수집: Luna-low → claude-sonnet-5-unknown_`.

The header preserves owned terminal async outcomes (`completed`, `error`, and
restart-recovered `unknown`) so failed/timeout lanes render as failures rather
than generic missing information. If a record is genuinely missing, pruned, or
cannot be authenticated to the parent session, it is labeled `위임 기록 만료 · 실행경로 미검증`; attribution is never invented.

Each authenticated async completion is claimed once through the profile-scoped plugin-state API, so a replayed completion marker is rejected, also after a gateway restart, for as long as its record is retained. Records are pruned by age beyond 100 entries; a marker replayed after its record was pruned, or while the state read fails, is not recognized as a replay.

## Safety boundaries

- Models, effort levels, and toolsets use normalized membership checks against allowlists under `plugins.entries.delegate-task-routing.settings`.
- When a task explicitly names toolsets and the parent exposes a concrete enabled-toolset list, the task cannot request a broader named set; the constructed child is checked again before launch.
- Provider, endpoint, credentials, ACP transport, and API mode cannot be selected by the model.
- `ultra` is not accepted as reasoning effort; parallel lanes are the orchestration mechanism.
- Routing state uses `contextvars`, so concurrent gateway sessions do not share task choices.
- Child results include requested values and locally observed child model, provider, effort, toolsets, and session ID. These fields are not remote-provider attestation.
- Routed operation is supported for the active single-profile deployment. The plugin patches process-global private Python functions and is not validated for different allowlists across multiple profiles in one gateway process.


## Settings and least privilege

Configure explicit allowlists under the plugin entry; do not copy this example
unchanged into production. Start with only models and toolsets that the parent
agent already exposes. A requested toolset is rejected when it is not in this
list, and when the parent exposes a concrete enabled-toolset list it must also
be a subset of that parent list. Provider, endpoint, credential, ACP transport,
and API-mode selection are never model-controlled.

```yaml
delegation:
  model: gpt-6.1-sol
plugins:
  entries:
    delegate-task-routing:
      settings:
        allowed_models:
          - claude-opus-5
          - claude-fable-5.1
          - gpt-6-astra
          - gpt-6.1-sol
          - gpt-6-luna
          - claude-sonnet-5
        allowed_reasoning_efforts: [low, medium, high]
        allowed_toolsets: [file, web]
```

Settings are read when the plugin loads. Use your Hermes configuration command
or reviewed configuration management, then restart or open a fresh CLI session
before relying on a changed allowlist. A multi-profile deployment needs a
separate gateway process per policy: this plugin patches process-global private
Python functions and has not been validated with different profile allowlists
in one process.

### Fixed work-type models and recovery

Every `route_turn` lane requires `work_type` with an exact enum value:

| Work type | Required primary model | Scope |
| --- | --- | --- |
| `implementation` | `claude-opus-5` | Code changes, file/sheet/doc writing, Figma edits, deploy preparation, any local/external write |
| `research` | `claude-opus-5` | Web research, DB query analysis, root-cause diagnosis, comparison/recommendation |
| `verification` | `gpt-6.1-sol` | Independent checks and reviews a user asks for |
| `mechanical` | Any allowlisted model | Extraction, reformatting, deterministic checks, simple visual QA |
| `architecture` | Any allowlisted model | Exceptional architecture/design |

A rejected route tells the model the required work type/model. The declared work
type is a semantic contract, not automatic classification of arbitrary goals.
Tasks need not carry `work_type`: matching remains label/model/effort/toolsets.

Partial `settings.fixed_models` overrides merge with the defaults below. Values
must be exact IDs present in `allowed_models`; unknown keys/invalid values fail
closed. Registered schema/guidance reflects the effective mapping at load time:

```yaml
fixed_models:
  implementation: claude-opus-5
  research: claude-opus-5
  verification: gpt-6.1-sol
```

This changes delegated lane models, not the parent model. Trivial safe work stays
direct; bounded work uses single; independent outcomes use parallel.

Recovery is one ordered chain. Each primary receives only the models **after** it:

| Primary | Ordered fallback models |
| --- | --- |
| `gpt-6.1-sol` | `gpt-6-sol`, `gpt-5.6-sol`, `gpt-6-luna`, `claude-opus-5`, `claude-sonnet-5` |
| `gpt-6-sol` | `gpt-5.6-sol`, `gpt-6-luna`, `claude-opus-5`, `claude-sonnet-5` |
| `gpt-5.6-sol` | `gpt-6-luna`, `claude-opus-5`, `claude-sonnet-5` |
| `gpt-6-luna` | `claude-opus-5`, `claude-sonnet-5` |
| `claude-opus-5` | `claude-sonnet-5` |
| `claude-sonnet-5` | None |

Astra, Fable and all other models outside this chain have **no automatic fallback**.
`gpt-6-sol` and `gpt-5.6-sol` are fallback-authorized without being selectable
primary lanes. Recovery authorization is the union of the separate chain-derived
fallback allowlist and `allowed_models`; narrowing primary choices does not remove
chain models from recovery. Explicit `fallback_providers: []` disables recovery.

Each entry declares `openai-codex` for `gpt-*` or `anthropic` for `claude-*`.
The core resolves credentials lazily through its shared provider runtime at
activation, derives the correct API mode, and walks onward on resolution/eligible
provider failures; the plugin does not copy credentials from the current lane.
Automatic suffixes cover either parent provider and include Anthropic primaries.
Operator-pinned provider, endpoint, key, API mode or ACP transport gets no implicit
recovery; explicit operator chains remain supported. Explicit chains are copied
without mutation and must use recovery-authorized IDs and matching providers;
malformed/unauthorized chains fail closed before construction. The primary runs
first and the native runtime decides which failures qualify for failover. There
is no verifier/implementer-model special case on fallback; actual-route metadata
discloses requested versus actual model/provider.

Availability checked before this update: both fallback-only Codex IDs appeared in
the authenticated catalog and tiny calls completed HTTP 200. `claude-opus-5`
appeared in the native OAuth catalog, but the tiny inference call returned HTTP
429; successful Opus inference remains unverified. No IDs were substituted.

The startup-captured schema, validator and imported plugin are **not hot-reloaded**.
Changing `delegation.model` may affect newly constructed children because the core
reads delegation settings per spawn, even while the gateway retains the old plugin
and schema. Therefore this local edit is prepared/tested, not a claim of coherent
live activation. After the operator's separate restart, verify fresh tool schemas
and actual requested→actual routes before declaring rollout complete.

`policy_bypass` is a protective degraded mode, not successful routing. If the
routing tool cannot be safely pinned into the available tool surface, or its
forced-call loop reaches its safety limit, the turn is marked `라우팅 정책 미적용`
and logged rather than being repeatedly retried. Per-task validation still
applies to any later delegation, but operators should investigate the Hermes
compatibility/tool-surface change before treating that turn as policy-enforced.

## Update behavior

Install the plugin in Hermes' persistent plugin directory, not the replaceable application image, so normal image replacement does not discard it. Behavioral compatibility with a later Hermes version is not implied.

At load time it verifies selected native function names and signatures. If those structural checks fail, the plugin fails closed instead of guessing. Because routing also depends on Hermes constructing children synchronously before background dispatch, upgrades still require behavioral smoke tests. If a future Hermes release exposes all four field names (`label`, `model`, `reasoning_effort`, `toolsets`) natively, the plugin does not patch the built-in implementation; operators must separately verify that the native policy semantics match this plugin.

Allowlist settings and the extended schema are captured when the plugin loads. Changing them requires a fresh CLI session or gateway restart.

## Verification

```bash
HERMES_SRC=/path/to/hermes HERMES_PYTHON=/path/to/hermes-python \
  ./scripts/verify.sh

# The integration script performs the installed private-symbol contract probe.
# It needs a local Hermes source tree and its matching Python environment.

```

## Disable / rollback

```bash
hermes plugins disable delegate-task-routing
```

A gateway restart or fresh CLI session is required after enable/disable. Disabling restores Hermes' built-in `delegate_task`; existing completed session records are not changed.

## Repository workflow

Fresh installation from a **separate source checkout** (not the live plugin path):

```bash
./scripts/verify.sh
./scripts/deploy.sh
./scripts/deploy.sh --apply --enable
```

The first deploy command is a dry run. `--apply` is mandatory for a write, and `--enable` uses Hermes' official CLI to enable the plugin and grant its declared `tools.override` capability. Unknown arguments fail without deploying. An existing installation with a **separate source checkout** that is already enabled may use `./scripts/deploy.sh --apply`.

Deployment stages a complete replacement, retains a timestamped backup, and verifies every installed byte. **When this git checkout itself is the live `$HERMES_HOME/plugins/delegate-task-routing`, do not run `--apply`: the deploy script refuses self-deployment to protect `.git`. Edits are already staged at the live path but are not imported by the running gateway.** It intentionally does not restart the gateway. A parent/operator must first independently review this diff and tests, snapshot the session store using the approved quiesced/atomic procedure, then restart from an external shell (not inside the gateway) and complete [docs/SLACK_SMOKE_TEST.md](docs/SLACK_SMOKE_TEST.md). Keep the prior version in an independently saved checkout/backup for rollback; do not assume self-deployment made one.

Rollback is also dry-run by default for separately deployed copies. **Do not use this replacement script on a live git checkout**; it moves the entire checkout including `.git`. For this in-place installation, preserve the checkout and restore only the verified previous plugin payload from an independently captured backup, or halt activation until a tested rollback artifact exists:

```bash
./scripts/rollback.sh --from $HERMES_HOME/plugins/.delegate-task-routing.backup-TIMESTAMP
./scripts/rollback.sh --from $HERMES_HOME/plugins/.delegate-task-routing.backup-TIMESTAMP --apply
```

Before a Hermes update:

```bash
./scripts/pre-update-check.sh
```

The generated report records the currently working Hermes/plugin pair. After the update, rerun verification and the Slack post-restart smoke test before declaring compatibility.

## Release and verification boundary

The repository CI is a static-integrity check (Python compilation, manifest URL,
and release-version consistency). It does **not** install Hermes or exercise its
private symbols. `scripts/verify.sh` is the supported integration gate: it
proves compilation, the complete automated policy suite, and compatibility with
the locally installed Hermes delegation symbols.
`scripts/pre-update-check.sh` additionally requires tested source and deployed
plugin bytes to match. Release tags use `vMAJOR.MINOR.PATCH`; never bypass a
failed private-symbol compatibility check.

Automated checks do **not** prove a real provider request, gateway restart
continuity, or Slack delivery. Those remain explicit release gates in
[docs/SLACK_SMOKE_TEST.md](docs/SLACK_SMOKE_TEST.md); do not describe a release
as fully operational until that checklist passes. Local child metadata is an
observation from the running process, not provider attestation.
