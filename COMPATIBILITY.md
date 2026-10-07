# Hermes compatibility

The plugin persists independently of the Hermes application image, but behavioral compatibility must be proven for every Hermes update.

| Plugin version | Hermes version | Upstream commit | Python | Status | Evidence |
|---|---|---|---|---|---|
| 0.3.3 | locally installed `/opt/hermes` | see local git revision | 3.13.5 | Prepared, activation pending | `scripts/verify.sh` contract suite, hermetic fenced reviewer output and single-verifier lane regressions; no gateway restart or live Slack test for this version |
| 0.2.9 | 0.21.1 | `fef0e16f` | 3.13.5 | Supported locally | 48 tests + installed contract probe; authenticated non-sensitive lifecycle and worker/verifier completion-chain ownership |
| 0.2.8 | 0.20.6 (2026.8.27) | `01740f352e2f2414b3bdb7791c2ed1f7bbb2b455` | 3.13.5 | Supported locally | 46 tests + installed contract probe; terminal outcomes, compression lineage, context-local turn identity |
| 0.2.6 | 0.20.6 (2026.8.27) | `01740f352e2f2414b3bdb7791c2ed1f7bbb2b455` | 3.13.5 | Supported locally | 40 tests + installed contract probe; includes non-sensitive worker/verifier phase contract |
| 0.2.5 | 0.20.6 (2026.8.27) | `01740f352e2f2414b3bdb7791c2ed1f7bbb2b455` | 3.13.5 | Supported locally | 39 tests + installed contract probe passed as part of extension-suite verification |
| 0.2.1 | 0.20.6 (2026.8.27) | `01740f352e2f2414b3bdb7791c2ed1f7bbb2b455` | 3.13.5 | Supported locally | Contract import passed; 27 tests passed before repository extraction |

## Compatibility contract

The plugin currently wraps these private symbols from `tools.delegate_tool`:

- `delegate_task`
- `_build_child_preserving_parent_tools`
- `_run_single_child`

A release is compatible only when all of the following pass:

1. Python compilation.
2. The complete plugin test suite, including the installed-Hermes contract test.
3. Plugin registration with the built-in-tool override grant.
4. A fresh-process direct routing smoke test.
5. After gateway restart, one new Slack direct turn and one harmless delegated turn with the expected actual-routing header.

## Status meanings

- **Supported locally**: verified against the exact locally installed version and upstream commit.
- **CI compatible**: repository CI passed against the named upstream ref.
- **Experimental**: latest upstream was tested, but support is not yet declared.
- **Blocked**: structural or behavioral contract failed; do not update the gateway.

## Update rule

Do not infer compatibility from unchanged filenames. Run `scripts/pre-update-check.sh` before upgrading, retain its report, update Hermes, rerun `scripts/verify.sh`, restart the gateway, then complete `docs/SLACK_SMOKE_TEST.md`. If any step fails, keep or restore the last supported Hermes/plugin pair.
