# Slack post-restart smoke test

This is a manual gateway acceptance test. A CLI command using `--source slack` still runs with `platform=cli` and is not proof of Slack enforcement.

## Preconditions

1. `scripts/verify.sh` passes against the installed Hermes tree.
2. When deploying from a separate source checkout, `scripts/deploy.sh` reports byte-for-byte success. When the live git checkout is the plugin itself, `--apply` is refused; verify this checkout in place and retain an out-of-tree backup before restart.
3. An operator independently verifies the staged diff and snapshots the session store safely before an **external** gateway restart (not `/restart` or `hermes gateway restart` in a gateway child). Inspect the journal mode with an immutable connection and sidecars by filesystem metadata only; quiesce writers before copying a DELETE-mode store, or use an atomic filesystem snapshot if quiescence cannot be established. In this s6 deployment use `/command/s6-svc -r /run/service/gateway-default` from a separate host shell (`s6-svc` is not on the gateway PATH), then verify the gateway starts and imports `delegate-task-routing@<PLUGIN_VERSION>` (the version in `plugin.yaml`). Do not restart while active children are running. Back up a verified prior plugin payload outside the live directory first; the present 0.3.0 dirty checkout was not captured before edits, so the older v0.2.9 backup is not an exact rollback.
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

## Test C — no automatic verification

Send a request whose wording used to force a verifier, against a disposable file or read-only target, for example:

```text
이 임시 파일의 배포 권한 설정을 설명해줘: routing smoke
```

Pass conditions:

1. `route_turn` accepts `direct`, `single` or `parallel`; `worker_verifier` is not offered in the tool schema.
2. No second delegation is dispatched after the worker completion turn.
3. The final answer carries no `검토 대기` / `검토 중` / `검토 실패` prefix.
4. `development_review` is not present in the session's tool list.

Then, in a follow-up message, ask explicitly for an independent check of a harmless result that already exists. It must run as one ordinary lane with `work_type=verification` on the fixed verification model, and the header names that lane.

Finally ask for a harmless piece of work and an independent review of it in one message. The work must be delegated alone, with no verification lane in the same plan, and the final answer must say that the review has not run.

If smoke fails, restore only the verified pre-change plugin payload in place, leaving the checkout's `.git` intact; never run `rollback.sh --apply` against this live checkout. An older backup (`/opt/data/plugins/.delegate-task-routing.backup-20260922T075725Z`) contains v0.2.9, **not** the exact pre-edit v0.3.0 and must not be represented as an exact rollback. If no verified pre-change payload exists, leave restart blocked or arrange an independently tested rollback package first. Restart externally after a safe session snapshot and repeat a fresh direct + review smoke. A passed local test is not live activation.

## Failure handling

- If `route_turn` is absent or another tool runs first, disable the plugin or roll back to the last compatible tag before production use.
- If the plugin fails to load after a Hermes update, do not bypass `_assert_compatible`; open an issue with the Hermes version, upstream commit, failing symbol, and test output.
- If a header is absent but execution succeeded, treat attribution as unverified rather than manually inventing a header.
