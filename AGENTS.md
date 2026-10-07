# AGENTS.md

Rules for any agent or person changing this repository. Read this before the first write.

Design, safety boundaries and deployment mechanics are in [README.md](README.md). Hermes compatibility is in [COMPATIBILITY.md](COMPATIBILITY.md). This file covers one thing: how versions, branches and tags are kept in step with the code.

## Where the version lives

A version is declared in five places, and they must always agree:

| Place | Form |
|---|---|
| `__init__.py` | `PLUGIN_VERSION = "X.Y.Z"` |
| `plugin.yaml` | `version: X.Y.Z` |
| `README.md` | ``Current release: `vX.Y.Z` `` |
| `CHANGELOG.md` | topmost `## [X.Y.Z] - YYYY-MM-DD` heading |
| git | annotated tag `vX.Y.Z` |

`scripts/check-version.sh` checks the first four on every push, and checks the tag name when CI runs for a tag. A version that has no tag is not a release.

## Choosing the number

- **Patch** (`0.4.0` → `0.4.1`): fixes that do not change routing policy, the tool schema or settings.
- **Minor** (`0.4.0` → `0.5.0`): any change to routing policy, model roster, recovery order, tool schema, settings or hooks.
- **Major**: reserved for `1.0.0` and later breaking changes.
- Changes to documentation, tests or CI only do not change the version and are not tagged.

One version number belongs to exactly one commit. Never reuse a number for different code, and never skip a number.

## Release procedure

Do these in order. Do not start the next version until the last step is done for the current one.

1. Create a new branch from an up-to-date `main`: `<type>/<short-topic>-<YYYYMMDD>`.
2. Make the change. In the final commit of the branch, set the new version in all four files and add the `CHANGELOG.md` section dated today.
3. Run `scripts/verify.sh` and `scripts/check-version.sh`. Both must pass.
4. Push the branch and open a pull request against `main`. CI must pass.
5. Merge with a **merge commit**. Do not squash or rebase: the tag must point at a commit that keeps its hash.
6. Tag the commit that set the version, and push the tag:

   ```bash
   git fetch origin
   git tag -a vX.Y.Z <release-commit> -m "delegate-task-routing vX.Y.Z"
   git push origin vX.Y.Z
   ```

   The tagged commit must be reachable from `origin/main`. Check with `git merge-base --is-ancestor vX.Y.Z origin/main`.
7. In the working checkout, return to `main` and fast-forward it: `git switch main && git pull --ff-only`.

A branch is finished once its pull request is merged. Do not push further commits to it; new work starts at step 1 on a new branch.

## Released is not the same as active

A tag says the code is in `main` and passed static checks. It does not say the running gateway has loaded it.

- On this installation the git checkout is the live plugin directory (see "Repository workflow" in the README). Files on disk are imported only when the gateway restarts.
- Record activation state per version in `COMPATIBILITY.md`, following the status meanings defined there.
- Do not describe a version as operational until `docs/SLACK_SMOKE_TEST.md` passes.

Because the checkout is live, keep it clean: no uncommitted changes left across sessions, and no feature branch left checked out after its pull request is merged.

## CHANGELOG rules

- The date in a heading is the date the version was tagged.
- `Unreleased` is allowed only on the topmost section, while its branch is still open. Replace it with the date before tagging.
- Never edit the section of a version that is already tagged, except to correct a factual error.

## Known gaps in history

Tags `v0.2.8` to `v0.3.2` were added retroactively on 2026-10-07. Two things could not be repaired and are left as they are:

- **`0.3.0` has no tag.** No commit ever declared it; the tree went from `0.2.9` straight to `0.3.1`.
- **`release/v0.2.9` (`614435e`) is not `v0.2.9`.** That branch was never merged. The tag `v0.2.9` points at `44fb3f6`, the `0.2.9` commit that is in `main`.
