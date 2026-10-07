# Changelog

All notable changes to this project are documented here.

## [0.5.0] - 2026-10-07

### Removed

- Remove the automatic high-risk verification stage. `route_turn` no longer accepts `worker_verifier`; the keyword classifiers that forced or forbade it are gone, and a worker completion no longer creates a forced verifier dispatch turn. Lanes no longer carry `phase`; a `phase` value sent by the model is ignored.
- Remove the task-level development review gate: the `development_review` tool, `route_turn.review_task_id`, the repository fingerprint, the injected review guidance, and the `검토 대기` / `검토 중` / `검토 실패` prefixes on final answers.

### Changed

- Verification runs only when a user asks for it, as an ordinary lane with `work_type=verification`. Work and a review of that work cannot run from one message: there is no sequential mode and a completion turn cannot dispatch further lanes, so the guidance tells the parent to delegate the work only and to say the review has not run; the user requests it in a follow-up message. `fixed_models`, the model roster and the recovery chain are unchanged.
- A delegation record left in `workers_dispatched` or `verifiers_dispatched` by an earlier version is claimed once as an ordinary completion; a legacy verifier completion keeps the stored worker routes and source delegation in the header and lifecycle view. All completion records are now prunable by age. The `development_reviews` state key is left in place and ignored.
- `delegation_phase_for_turn` always returns `worker`; `delegation_lifecycle_for_turn` no longer reports a `verification` mode.
- Cap `delegate_task` forcing at three forced calls per accepted plan. A `single`/`parallel` plan that has still not dispatched (rejected tasks, or `list`/`steer`/`stop` calls) is no longer forced, so the parent can report the blocker in text or call `route_turn` again instead of repeating the call until the core `identical_call_streak_halt` guardrail ends the turn without an answer. The plan and the pre-tool gate stay in force; the rejection and gate messages say that forcing was released. `route_turn` forcing already had the same three-call limit. A final answer sent from a released, undispatched plan gets the header `위임 미실행` rather than the declared lanes marked `실행 중`.

### Verification

- Removed `tests/test_development_review.py` and `tests/test_risk_policy.py`; added regressions for the removed mode, ignored `phase`, wording that used to force verification, legacy-record claims and replay rejection. The core/registry/positional/JSON dispatch-and-completion contract test moved to `tests/test_routing.py` and now covers an ordinary `single` plan.
- `scripts/verify.sh` passed in the gateway container against the installed Hermes from a separate checkout of this branch at `75bfe2a`: 189 tests and the installed private-symbol contract. The gateway restart and the Slack smoke test are pending.

## [0.4.0] - 2026-10-07

### Changed

- Replace the previous Opus roster ID with `claude-opus-5`. Require per-lane `work_type`: implementation/research use Opus 5; verification uses `gpt-6.1-sol`; mechanical/architecture remain free among allowlisted models. Enforce verification on the original verifier phase and on every checkpoint review lane before normalizing dispatch. `settings.fixed_models` supports validated partial overrides, reflected in the registered schema and guidance. Task matching remains label/model/effort/toolsets; tasks need not carry `work_type`.
- Replace single-peer recovery with each model's ordered suffix of `gpt-6.1-sol → gpt-6-sol → gpt-5.6-sol → gpt-6-luna → claude-opus-5 → claude-sonnet-5`. Astra, Fable and other models outside this chain have no automatic fallback. Separate recovery authorization accepts fallback-only Sol generations without authorizing them as primary lanes. Provider entries use `openai-codex`/`anthropic` and native lazy credential resolution; explicit chains still validate fail-closed, explicit `[]` disables recovery, and trusted transport/credential overrides gain no implicit recovery.

### Verification

- Added enforcement, configuration/schema override, task matching, every recovery position, same/cross-provider construction and installed-core normalization regressions; retained v0.3.x review-gate tests.
- Live provider catalogs list `gpt-6-sol`, `gpt-5.6-sol` and `claude-opus-5`. Tiny Codex calls completed HTTP 200; Opus 5 native OAuth inference hit HTTP 429 (availability is catalog-confirmed, successful inference remains unverified). A raw API-key-style Anthropic probe returned HTTP 401 before switching to the actual native OAuth adapter.
- Gateway restart and fresh Slack routing/actual-failover smoke tests remain operator-owned and pending. No Hermes core changes or PR.

## [0.3.3] - 2026-10-07

### Fixed

- Parse reviewer summaries through Hermes' `extract_json_candidate`, with an identical local fallback when the helper cannot be imported. Markdown-fenced JSON and surrounding prose now follow the core output validator's extraction rules; exact verdict schema, independent child/extension authentication and both revision checks remain mandatory, including for schema-less results.
- Accept a single checkpoint reviewer lane labeled `verifier` when `review_task_id` is supplied, normalizing it to `worker` for unchanged dispatch semantics. Ordinary single/parallel lane restrictions and mandatory high-risk worker/verifier routing remain unchanged. Updated `route_turn` guidance and schema description.

### Tests

- Added fenced pass/fail, revision mismatch, non-JSON prose, artifact mutation, authentication, live Slack summary-shape, fallback/core extraction parity and reviewer dispatch/guard regressions; expanded schema-less exact-contract checks to fenced/prose wrappers.
- Gateway activation and a fresh live Slack smoke test remain pending; no Hermes core or live plugin-state changes.

## [0.3.2] - 2026-10-07

### Fixed

- Inject the read-only reviewer goal and per-task JSON output contract in the patched native `delegate_task` entrypoint used by the live core dispatcher, with a pre-launch artifact revision recheck. The registry handler no longer duplicates injection or dispatch bookkeeping.
- Record terminal malformed/failed reviews as `review_failed`, with a short reason, findings and a reviewer summary capped at 500 characters, instead of silently cycling back to `review_due`. Accept authenticated schema-less results only when they match the exact verdict JSON contract and revision. Failed reviews still do not grant completion readiness; checkpoint explicitly to retry.
- Preserve assistant messages and progress updates, prepending one short Korean review status line. Readiness-check exceptions also preserve the body. Existing routing header format is unchanged.

### Tests

- Added regressions through the patched core entrypoint (not just the registry), plus schema coercion, pre-launch revision changes, failed verdict/malformed output, strict schema-less acceptance, body-preserving status/errors.
- No gateway restart or Hermes core changes; live Slack activation remains pending.

## [0.3.1] - 2026-10-02

- Added profile-scoped development task review checkpoints, exact git-tree fingerprints, independent single-lane review verdict authenticated from the core async ledger, safe progress and fail-closed final-output readiness for scoped Slack development tasks. Reviewed revisions are reused; later changes invalidate readiness. High-risk worker/verifier routing remains mandatory.
- Added hermetic review-gate contracts and a self-deployment refusal for installations where the git checkout is the live plugin directory. Gateway activation and platform smoke testing remain pending.

## [0.3.0] - Never tagged (shipped as part of 0.3.1)

### Changed

- Default model roster is exactly Opus 5, Fable 5.1, GPT-6 Astra, Sol, Luna and Sonnet 5. Guidance assigns Luna simple work, Sol general work (unchanged delegation default), Sonnet the general/medium Claude alternative, Opus critical independent verification/Codex recovery, Astra exceptional architecture, and Fable extreme long context. Existing direct/single/parallel/worker_verifier risk policy is unchanged.
- Cross-provider routed lanes resolve the target provider's complete credential bundle instead of inheriting the wrong endpoint. Automatic recovery is now independent of that resolution and also covers same-provider Codex parent/child routes.
- Exact recovery map: GPT-6 Luna→Sonnet, Sol→Opus, Astra→Fable. Sonnet is explicitly authorized as the sixth model and supplies cost-appropriate Luna outage recovery. Unknown IDs have no guessed peer; absent peers disable automatic fallback.
- Explicit fallback chains must use allowlisted models and matching native providers. Malformed or unauthorized chains fail closed; explicit disable and trusted credential/transport overrides are respected without shared-config mutation.
- Local manifest/documentation version aligned with the existing 0.3.0 code. No external release or gateway restart; fresh live schema/actual-route verification remains pending operator activation.

### Tests

- Added focused roster, exact-peer, same/cross-provider recovery, allowlist, trusted-override, native-chain normalization and risk-policy regressions.

## [0.2.9] - 2026-09-21

### Added

- An authenticated, non-sensitive `delegation_lifecycle_for_turn` observer contract.
- Worker/verifier completion-chain ownership so presentation plugins finalize only the delegations owned by the authenticated completion turn.
- Durable one-time completion claims reject replayed envelopes, and retention prunes only consumed terminal records so live mandatory-verification chains cannot disappear.
- `route_turn` guidance now documents `claude-opus-5`, `claude-sonnet-5`, and `claude-fable-5.1` (anthropic) as selectable child lanes: sonnet-5 as a Terra-class general lane, opus-5 as a Sol-class high-quality/high-risk lane, and fable-5.1 (1M context) as an Astra-class very-large-context lane. Claude lanes are the preferred stable path when codex-family lanes are rate-limited (HTTP 429) or slow to first token. Operators must also list each model in the `allowed_models` setting for it to be selectable.

## [0.2.8] - 2026-09-09

### Fixed

- Authenticate owned `completed`, `error`, and `unknown` async outcomes across
  verified compression continuations while rejecting branches and subagents.
- Prefer explicit or context-local turn identity for final headers, preventing
  same-session interleaving from producing false unverified-route labels.
- Use `위임 기록 만료 · 실행경로 미검증` only for genuinely unverified records.

## [0.2.6] - 2026-09-09

### Added

- A read-only `delegation_phase_for_turn` contract exposing only `worker` or
  `verifier` so presentation plugins can show accurate verification status
  without reading goals, labels, models, or tool arguments.

## [0.2.5] - 2026-09-04

### Fixed

- **Duplicate answers / trailing background agents on trivial questions.**
  The model would declare `single`/`parallel` for questions it could answer
  immediately, answer inline in the interim message, and then answer again
  in the async completion turn — leaving "working…" indicators and doubling
  token spend. Three coordinated changes:
  - Schema now says delegation runs in the background and sends a second
    message, so questions answerable from context/skills must be `direct`;
    "when uncertain choose the higher mode" is scoped to RISK only.
  - route_turn acceptance for delegated modes now instructs the model to
    send only a one-line interim status and leave the full answer to the
    completion turn.
  - The async-completion context invites an exact `NO_REPLY` when the
    interim message already fully answered, so the gateway suppresses the
    duplicate; `transform_llm_output` no longer prepends the Alex header to
    silence markers (a header would have broken the gateway's suppression
    match and delivered the marker as a message).

## [0.2.4] - 2026-09-04

### Fixed

- **Completion/verification plans can no longer be clobbered by a redundant
  route_turn call.** In an async-delegation completion turn the model would
  habitually call route_turn again; its accepted "direct" plan overwrote the
  completion plan, so the final header claimed `직접 처리` instead of
  attributing the actual delegated lanes (and, in worker_verifier flows,
  could erase the forced verifier dispatch state). route_turn now returns
  `already_routed` without touching the recorded plan, and the async
  completion/verification context messages explicitly instruct the model not
  to call route_turn in those turns.
- **Header `Sol-unknown`.** registry.dispatch passes no agent object to tool
  handlers, so the main agent's reasoning effort was unreadable. The
  middleware now records the effort observed in each Slack parent session's
  own LLM request payload and the header uses that locally observed value;
  when the payload carries none the header still says `unknown` rather than
  inventing a number.

### Changed

- route_turn schema now encodes the lane model-assignment policy (Luna for
  trivial/mechanical, Terra for general analysis/code, Sol only for
  high-risk or final adversarial verification; identical tasks get identical
  models) and states explicitly that `mode=direct` requires `lanes: []` and
  that route_turn must be called at most once per turn — reducing the
  recurring first-attempt "direct mode must have no lanes" validation retry.

## [0.2.3] - 2026-09-04

### Fixed

- **Forced route_turn loop (stuck "working…" status, duplicate replies,
  iteration-budget burn).** `registry.dispatch` invokes plain tool handlers
  with only `task_id`/`session_id`/`user_task` — no `parent_agent` and no
  `turn_id` (agent-loop tools like `delegate_task` are the exception). The
  route_turn handler therefore stored its plan under a `("session", …)` key
  while the LLM middleware and hooks look plans up under the `("turn", …)`
  key, so the middleware never saw the accepted plan and re-forced
  `route_turn` on every subsequent API call of the turn until the iteration
  budget ran out. The handler now resolves the turn id from the session→turn
  map maintained by the pre-LLM hook, landing the plan under the key the
  middleware actually reads.
- Added a forcing loop breaker: if route_turn has been forced
  3 times within one turn without a stored plan (any cause — key drift,
  repeated validation failures), the turn falls back to `policy_bypass`
  (header `라우팅 정책 미적용`, warning logged) instead of looping.
- Bounded `_FORCED_ROUTE_COUNTS` / `_FORCED_ROUTE_REQUESTS` growth.

## [0.2.2] - 2026-09-04

### Fixed

- **Slack outage under Tool Search (`tools.tool_search.enabled: auto`).**
  v0.2.1 injected the `route_turn` declaration into the provider request and
  forced its selection, but Tool Search classifies non-core plugin tools as
  deferrable, so the executor's collapsed tool surface rejected the forced
  call (`Tool 'route_turn' does not exist`) and every normal Slack turn died
  after three retries. `route_turn` is now pinned into the core (never
  deferred) tool list at load time, and the pin is reverted on unload.
- Added a request-time fail-safe: if `route_turn` is still deferrable when a
  turn starts, the per-turn orchestration policy is skipped for that turn
  (recorded as `policy_bypass`, header `라우팅 정책 미적용`, warning logged)
  instead of killing the turn. Per-task model/effort/toolset routing still
  applies to any delegation the model dispatches in a bypassed turn.

### Known limitations

- The core-tool pin mutates `toolsets._HERMES_CORE_TOOLS` (a process-global
  list); a future Hermes layout without that list falls back to the
  request-time bypass path.

## [0.2.1] - 2026-09-03

### Added

- Mandatory per-turn `route_turn` policy for top-level Slack sessions.
- `direct`, `single`, `parallel`, and sequenced `worker_verifier` execution modes.
- Mandatory `goal`, `label`, `model`, `reasoning_effort`, and `toolsets` fields for delegated tasks.
- Exact plan-to-dispatch validation and parent-permission checks.
- Provider-native forced tool declarations and choices for Chat Completions, Codex Responses, Anthropic Messages, and Bedrock Converse.
- Durable worker-to-verifier continuation state and same-parent completion authentication.
- Deterministic `_Alex: ..._` headers based on locally observed routing metadata.
- Regression tests for forged completions, persistence failure, fallback display, and provider request shapes.
- Safe staged deployment with explicit apply/enable flags, retained backups, and executable rollback.
- Declared `tools.override` capability, hook metadata, portable tests, compatibility CI, release archives, and SHA-256 checksums.
- Source-to-deployed byte comparison in the pre-update gate.

### Security

- Reject unverified, incomplete, or cross-session async completion markers.
- Fail closed when mandatory verifier continuation state cannot be persisted.

### Known limitations

- Uses private Hermes delegation symbols and therefore requires compatibility testing after Hermes updates.
- `transform_llm_output` cannot guarantee a header for interrupted/empty responses or against an earlier winning transform.
- Actual model metadata is locally observed child state, not provider-request attestation.
- Semantic complexity classification cannot be made perfectly deterministic.

## [0.2.0] - 2026-09-03

- Initial strict orchestration-policy implementation derived from the local per-task routing extension.
