# Slack post-restart smoke test

This is a manual gateway acceptance test. A CLI command using `--source slack` still runs with `platform=cli` and is not proof of Slack enforcement.

## Preconditions

1. `scripts/verify.sh` passes against the installed Hermes tree.
2. `scripts/deploy.sh` reports byte-for-byte deployment success.
3. An operator runs `/restart` in Slack.
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

## Failure handling

- If `route_turn` is absent or another tool runs first, disable the plugin or roll back to the last compatible tag before production use.
- If the plugin fails to load after a Hermes update, do not bypass `_assert_compatible`; open an issue with the Hermes version, upstream commit, failing symbol, and test output.
- If a header is absent but execution succeeded, treat attribution as unverified rather than manually inventing a header.
