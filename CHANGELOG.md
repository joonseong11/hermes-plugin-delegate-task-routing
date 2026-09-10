# Changelog

All notable changes to this project are documented here.

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
