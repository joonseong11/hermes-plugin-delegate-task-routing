# delegate-task-routing

Current release: `v0.3.2` (local working version, unpublished; gateway activation pending). See [COMPATIBILITY.md](COMPATIBILITY.md) before changing the Hermes version and [CHANGELOG.md](CHANGELOG.md) for release history.

Persistent, in-process orchestration policy for Hermes. It forces a per-turn execution decision on Slack parent sessions, applies exact-allowlisted per-task routing, records requested versus actual child execution, and deterministically prepends the `_Alex: ..._` execution header.

## Per-turn policy

### Localized Figma edits and refreshed skills

Clear icon/color/copy/alignment/spacing/visibility edits use direct execution when they do not change shared component definitions, Variant/Property schemas, navigation contracts, or permissions. The parent reloads user-requested changed skills, preserves user changes, records each external write, and reads back and visually checks every changed screen. Repeating the same local change on several screens is not itself a reason to delegate.

Routing remains the first tool call. A requested skill refresh selects direct with empty lanes before skill_view. If that read reveals higher-risk work, the parent reroutes before any write. Already-delegated work and completion/verifier turns retain their existing routing; this is not a general route-reset permission.

This is model-facing classification guidance, not an automatic Figma risk classifier. Its effect must be checked in actual task traces. Changing these Python files does not hot-reload an already imported plugin; no gateway restart is performed as part of this file-only change.

Every normal Slack parent turn must call `route_turn` first. LLM request middleware forces that exact tool choice until a valid decision exists:

- `direct`: trivial lookup, brief judgment, bounded read-only checks, or small reversible local edits; no delegation lanes.
- `single`: substantial general research/implementation in exactly one worker lane, including its own focused tests. Parent reviews evidence; a second verifier is not automatic.
- `parallel`: at least two genuinely independent outcomes. Implementation and its dependent tests are not separate parallel outcomes.
- `worker_verifier`: separate execution/review for external sends/posts/publishing/submission, production changes/deployments, money/accounting, legal/security/access work, irreversible effects or genuinely high-error-cost output. Changes to routing/verification policy itself belong here, even in a local file.

### Task-level development review (v0.3.2)

For recognized reversible local code-development requests, `pre_llm_call` creates a profile-scoped task ID. This **does not** change the per-turn route: direct/single/parallel remain available for ordinary work; high-risk `worker_verifier` always takes precedence (including authentication/permissions, payments/refunds, routing policy, production DB, deployment and external writes). Register a git repository root with `development_review(action=begin, root=<absolute git top-level>)` after `route_turn`. A task retains `task_id`, phase, checkpoint fingerprint, reviewed fingerprint and trigger (`milestone` or `completion`) in profile-scoped plugin state. Work may continue with normal tools and focused tests. Responses, including `development_review(action=progress)` turns, retain their original body; a short Korean status line discloses pending or failed review without suppressing the conversation.

At a meaningful milestone or before final readiness, call `development_review(action=checkpoint, task_id=..., trigger=milestone|completion)`. If the current revision is already reviewed, `ready` remains true without a repeat review. Otherwise call `route_turn(mode=single, review_task_id=..., lanes=[one reviewer])`, then `delegate_task` using that accepted lane contract. The patched native entrypoint supplies the read-only independent review goal and JSON output schema after the core dispatcher strips hidden fields, rechecking the artifact before launch. The parent may send an interim progress message while the child runs. The authenticated core async event must have `state=completed`, the same parent (or verified compression tip), one independent child, completed exit, a valid JSON object containing exactly `verdict`, `revision`, and string `findings`, `verdict=pass`, the frozen fingerprint, and an unchanged repository snapshot. `schema_valid=true` is expected; older results with `schema_valid=null` are accepted only if they satisfy the same exact JSON contract. Only then does `development_review(action=ready)` return ready for the exact artifact. A later edit or commit invalidates it; checkpoint again. Terminal malformed output or a failed verdict is recorded as `review_failed` with a reason, findings and a summary excerpt capped at 500 characters, not silently retried. Checkpoint explicitly for a new review. Failed or pending reviews cannot authorize completion. After reporting a reviewed task, `development_review(action=close)` ends its active lifecycle; this only succeeds for the reviewed revision. Closed tasks may still disclose later artifact changes, but records older than 72 hours are ignored by lookup and presentation, and open expired records auto-close on the next review save.

Fingerprint scope is repository `HEAD` and all non-ignored tracked/untracked file contents, names, deletions and symlink targets. Ignored/generated files and external services are outside this gate. The development-intent classifier is a deterministic backstop, not semantic proof that every coding paraphrase was detected: explicitly call `begin` for a missed task. The final-output transform is **status presentation**, not a general re-entry or prose-suppression hook: core `agent/turn_finalizer.py` runs the first nonempty `transform_llm_output` on truthy, uninterrupted responses only, and another earlier transform or streaming preview may bypass this transform. Registration, review dispatch, exact-artifact readiness and core-ledger authentication are enforced at plugin tool/async boundaries. Do not represent the local tests as unconditional pre-delivery enforcement across all delivery paths.

### Narrowed deterministic boundary (staged)

Bare `운영`, `production`, and `prod` previously forced a verifier even in an operations/fast-mode explanation or production-log lookup. They now permit routine routing **only with a read/explanation cue and no mutation cue anywhere in the request**. Mixed read-and-write requests, including later clauses/newlines, retain worker+verifier. Ambiguous operational requests without a clear read-only cue remain forced-high; other unclassified requests retain the existing semantic decision (`None`) rather than forbidding conservative verification.

All other high-risk keywords still take precedence over local/draft/test/read cues. This deliberately does not relax legal/security/accounting reviews or external writes, and it does not treat quoted/negated `deploy`, `publish`, etc. as safe. The classifier is a lexical backstop, not a complete intent parser: an unrecognized mutation paraphrase can escape detection, while a discussion containing mutation/risk terms can still over-escalate. The model must assess actual side effects and error cost; mode/count validation cannot prove that parallel outcomes are independent. No model roster, fallback/provider settings, fast mode, permission checks or verifier-continuation machinery changed.

`tests/test_risk_policy.py` covers Korean operation discussion, read-only production checks, routine local implementation, mixed production writes, external messages/writes, legal/security/money, routing-policy edits and conservative ambiguity. These are local policy tests, not a measured live latency improvement. Gateway restart and fresh-session routing smoke tests remain pending.

After a delegated decision, the next model call is forced to `delegate_task`. A `pre_tool_call` gate blocks unrelated tools between the decision and dispatch. Subagents and non-Slack runtimes are excluded from the parent-turn gate and from Alex headers.

## Added task fields

```json
{
  "goal": "Analyze the operational risk",
  "label": "위험분석",
  "model": "gpt-6.1-sol",
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

`worker_verifier` is sequenced across two durable async delegations. The first dispatch contains worker lanes only. Its completion event creates a new policy turn that forces the declared verifier lanes, injects the worker result into that turn, and prevents final-completion reporting until the verifier delegation returns. The worker and verifier route records are then merged for the final header. The continuation record is stored through the profile-scoped plugin-state API so a gateway restart between stages does not silently collapse the plan.

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
          - claude-opus-4-8
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

### Six-model selection and recovery

| Model | Intended lane |
| --- | --- |
| `gpt-6-luna` | Simple extraction, formatting, mechanical checks; low/medium effort |
| `gpt-6.1-sol` | General analysis, research, implementation; medium/high; delegation default |
| `claude-sonnet-5` | General/medium Claude alternative to Sol; cost-appropriate Luna outage recovery |
| `claude-opus-4-8` | Critical independent verification and Codex recovery |
| `gpt-6-astra` | Exceptional architecture and unusually difficult synthesis |
| `claude-fable-5.1` | Extreme long-context integration; Astra recovery |

This changes model choice, not risk policy: trivial safe work stays direct;
bounded work uses single; independent outcomes use parallel; consequential
external writes, production, money, legal, security and ambiguous high-risk work
retain worker_verifier with separate execution and verification phases.

Automatic peers are exact IDs: Luna→Sonnet, Sol→Opus, Astra→Fable. Sonnet is
explicitly authorized as the sixth model and avoids escalating simple Luna work
to Opus or Fable on outage. Sol remains the delegation default.
Unknown/retired IDs have no automatic peer. Peers omitted from `allowed_models`
are not used. The primary runs first; the native runtime decides whether a failure
qualifies for fallback, and actual-route metadata exposes any model change.

Previously automatic recovery was nested inside cross-provider credential
resolution and did not cover a Codex parent with a Codex child. Recovery now
covers both parent-provider cases. Credential resolution itself still only runs
when crossing providers without a trusted override. Operator-pinned provider,
endpoint, credential, API mode or ACP transport gets no implicit recovery route;
an explicit `fallback_providers` chain remains possible. Explicit `[]` disables
fallback. Explicit chains are preserved without shared mutation but must have
allowlisted models and their matching native providers; malformed or unauthorized
entries fail closed before construction. Credential-resolution failures also fail
closed (they are not automatic provider retries). No Anthropic→Codex or unknown
model fallback is invented.

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
