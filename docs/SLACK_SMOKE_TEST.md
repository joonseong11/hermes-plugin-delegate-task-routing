# Slack post-restart smoke test

This is a manual gateway acceptance test. A CLI command using `--source slack` still runs with `platform=cli` and is not proof of Slack enforcement.

## Preconditions

1. `scripts/verify.sh` passes against the installed Hermes tree.
2. When deploying from a separate source checkout, `scripts/deploy.sh` reports byte-for-byte success. When the live git checkout is the plugin itself, `--apply` is refused; verify this checkout in place and retain an out-of-tree backup before restart.
3. An operator independently verifies the staged diff and snapshots the session store safely before an **external** gateway restart (not `/restart` or `hermes gateway restart` in a gateway child). Inspect the journal mode with an immutable connection and sidecars by filesystem metadata only; quiesce writers before copying a DELETE-mode store, or use an atomic filesystem snapshot if quiescence cannot be established. In this s6 deployment use `/command/s6-svc -r /run/service/gateway-default` from a separate host shell (`s6-svc` is not on the gateway PATH), then verify the gateway starts and imports `delegate-task-routing@0.3.1`. Do not restart while active children are running. Back up a verified prior plugin payload outside the live directory first; the present 0.3.0 dirty checkout was not captured before edits, so the older v0.2.9 backup is not an exact rollback.
4. Start a new Slack conversation or thread after restart; do not reuse a cached pre-restart agent.

## Test A — direct

Send:

```text
7을 단어로만 답해줘.
```

Pass conditions:

- The final response starts with `_Alex: Sol-<actual effort> · 직접 처리_`.
- No delegated lane is claimed.
- Gateway logs show `route_turn` before any other model tool for that turn.

## Test B — harmless delegation

Send:

```text
서로 독립적인 두 작업으로 처리해줘. 첫 작업은 이 문장의 글자 수를 세고, 둘째 작업은 문장을 역순으로 적어줘: routing smoke
```

Pass conditions:

- A valid `route_turn` precedes `delegate_task`.
- Every child task includes `goal`, `label`, `model`, `reasoning_effort`, and `toolsets`.
- The final header names both lanes and reflects actual completion metadata.
- Any model/effort fallback is rendered as `requested → actual`; missing actual effort remains `unknown`.

## Test C — worker/verifier ordering

Use only a disposable file or read-only task. Confirm from logs and the durable delegation records that:

1. Worker delegation is dispatched first.
2. Verifier is absent from that first batch.
3. The completed worker record belongs to the same parent session.
4. Verifier dispatch occurs only in the completion-triggered continuation turn.
5. The final header merges worker and verifier routing records.

## Test D — task-level review (disposable git checkout only)

In a new Slack thread, request a small reversible local code edit in a disposable git repository. Observe auto-created `task_id`; call `development_review(begin, root=<repository top>)`, edit and test locally, then `checkpoint(trigger=completion)`. A progress message before review must not claim completion. Dispatch one independent reviewer using `route_turn(single, review_task_id=<task_id>)` and its accepted lane via `delegate_task`; verify exact child metadata and core-ledger completion. After a pass, `ready` must equal true for the reviewed fingerprint. Repeat checkpoint without edits: no second reviewer. Change a file: ready must fail until another review. After a fresh review, close the task and verify a later edit still invalidates its final readiness; start a new task ID in the same session. Separately ask for authentication/payment/deployment mixed with local code and verify `worker_verifier` remains mandatory. Do not perform real external writes for this smoke.

Review results are read through the core `get_durable_delegation` API, which includes uncheckpointed WAL frames; a missing core result must fail closed. Repeat the review on a compression-continuation session and verify the same task remains owned while a forked sibling cannot use it. Compression ownership resolution still uses an immutable session-tree read and must be checked against the live journal mode. The existing non-review worker/verifier continuation also uses an immutable ledger read: explicitly test its fresh async completion under the live journal mode before declaring **any** high-risk route operational. If WAL hides a required row, stop rollout and implement a core-owned event/ownership reader instead of bypassing mandatory verification.

If smoke fails, restore only the verified pre-change plugin payload in place, leaving the checkout's `.git` intact; never run `rollback.sh --apply` against this live checkout. An older backup (`/opt/data/plugins/.delegate-task-routing.backup-20260922T075725Z`) contains v0.2.9, **not** the exact pre-edit v0.3.0 and must not be represented as an exact rollback. If no verified pre-change payload exists, leave restart blocked or arrange an independently tested rollback package first. Restart externally after a safe session snapshot and repeat a fresh direct + review smoke. A passed local test is not live activation.

## Failure handling

- If `route_turn` is absent or another tool runs first, disable the plugin or roll back to the last compatible tag before production use.
- If the plugin fails to load after a Hermes update, do not bypass `_assert_compatible`; open an issue with the Hermes version, upstream commit, failing symbol, and test output.
- If a header is absent but execution succeeded, treat attribution as unverified rather than manually inventing a header.
