---
name: fork-update
description: "Use when syncing a personal Hermes fork's published main branch with upstream/main and optionally a local customization branch, then verifying and publishing the result."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [git, github, fork, upstream, update-channel, hermes-agent, release]
    related_skills: [hermes-agent, requesting-code-review, writing-plans]
---

# Fork Update

Use this skill when the user maintains a **personal Hermes fork as a shared update channel** and wants Hermes to perform the integration work:

- fetch the latest official `upstream/main`
- combine it with the fork's published `origin/main`
- optionally merge a local customization branch such as `local/solstice`
- resolve conflicts
- verify the result
- publish the verified result back to the fork's `main`

This skill is for the **publisher/maintainer** of the fork, not for consumers. Consumers should keep using ordinary `hermes update` against the fork remote.

## When to use

Load this skill when the user says things like:

- "sync my fork with upstream"
- "publish the latest official Hermes updates into my fork"
- "merge upstream/main and my local branch, then push main"
- "refresh my personal update channel"
- "/fork-update"

## When NOT to use

- A consumer machine just needs the latest published fork build — use `hermes update`
- The repo is not a fork / has no `origin` + `upstream` remotes yet
- The user wants manual git coaching only, not execution
- The current source branch has important **uncommitted** work that should not be published yet

## Default behavior

Unless the user says otherwise:

- Treat `origin` as the personal fork and `upstream` as the official Hermes repo
- Treat the current branch as the **source branch** when the current branch is not `main`
- If already on `main`, do an **upstream-only** sync unless the user names another source branch
- Publish to `origin/main` after verification

Supported intent modifiers from the user's trailing instruction:

- `dry-run` / `no-push` — do everything except the final push
- `source=<branch>` — merge that local branch instead of the current branch
- `upstream-only` — merge only `upstream/main` into the published channel
- `keep-worktree` — on success or failure, keep the temporary worktree for inspection

If the user does not specify these phrases, infer the safe default.

## Hard safety rules

1. **Inspect before mutating**
   - Run `git status --short --branch`
   - Run `git remote -v`
   - Run `git branch -vv`
   - Confirm `origin` and `upstream` both exist

2. **Do not silently publish uncommitted work**
   - If the chosen source branch has a dirty tree, stop and report it
   - Do not auto-commit or auto-push dirty working tree content unless the user explicitly says to include it

3. **Do not merge directly on the user's active worktree**
   - Use a temporary branch + temporary worktree
   - Keep the user's current checkout untouched

4. **Verify before push**
   - `git diff --check`
   - syntax/parse checks for changed Python or YAML files
   - targeted tests for the touched area
   - if verification fails, do not push

5. **Prefer preserving upstream structure**
   - When conflicts happen, keep upstream refactors/renames/layout changes
   - Reapply the fork's custom intent into the new upstream structure
   - Do not resurrect deleted old code just because it existed locally

## Recommended execution flow

### 1. Inspect repository state

Start with:

```bash
git status --short --branch
git remote -v
git branch -vv
git rev-parse --abbrev-ref HEAD
git fetch origin upstream
```

Verify:

- `origin` points to the user's fork
- `upstream` points to the official Hermes repository
- the source branch exists locally
- the source branch is clean unless the user explicitly wants to include dirty work

If `origin`/`upstream` are inverted or missing, fix that first before attempting sync.

### 2. Decide the source branch

Default rules:

- current branch != `main` → source branch = current branch
- current branch == `main` → source branch = none
- user passed `source=<branch>` → use that branch explicitly
- user passed `upstream-only` → source branch = none

Report the decision before mutating anything.

### 3. Create an isolated worktree

Use a timestamped branch and worktree so the user's real checkout stays untouched.

Example pattern:

```bash
SYNC_TS=$(date +%Y%m%d-%H%M%S)
SYNC_BRANCH="sync/fork-update-$SYNC_TS"
SYNC_DIR="$(dirname "$PWD")/hermes-agent-fork-update-$SYNC_TS"

git fetch origin upstream
git worktree add -b "$SYNC_BRANCH" "$SYNC_DIR" origin/main
```

Why `origin/main` as the base:

- it preserves whatever is already published in the fork's update channel
- then `upstream/main` is merged into that published state
- then the optional local customization branch is merged on top

### 4. Perform the merges

Inside the temporary worktree:

```bash
cd "$SYNC_DIR"
git merge --no-edit upstream/main
# if a source branch is selected:
git merge --no-edit "$SOURCE_BRANCH"
```

If both merges are clean, continue.

If conflicts occur:

1. inspect conflicted files with `git status --short`
2. read the files and understand what upstream changed structurally
3. resolve file-by-file using Hermes file tools
4. `git add <resolved-files>`
5. `git commit` to complete the merge only after the files are actually clean

### 5. Resolve conflicts intelligently

During conflict resolution:

- prefer the **new upstream architecture**
- port local behavior into the new locations
- preserve the fork's intended behavior, not necessarily its exact old lines
- re-read surrounding code after each conflict to confirm imports, call sites, and tests still make sense

When the fork has user-facing localization, prompts, or gateway behavior patches, verify that the behavior survives the refactor rather than blindly choosing `ours` or `theirs`.

## Verification checklist

Run verification from the temporary worktree, not the original checkout.

### Minimum required

```bash
git diff --check
```

### Python syntax for changed files

Only compile changed Python files if any changed:

```bash
CHANGED_PY=$(git diff --name-only --diff-filter=ACMR origin/main...HEAD -- '*.py')
if [ -n "$CHANGED_PY" ]; then
  python -m py_compile $CHANGED_PY
fi
```

### YAML validation when relevant

If changed files include YAML skill/config/locale files, parse them:

```bash
python - <<'PY'
import pathlib, yaml, subprocess
changed = subprocess.check_output(
    "git diff --name-only --diff-filter=ACMR origin/main...HEAD",
    shell=True,
    text=True,
).splitlines()
for rel in changed:
    if rel.endswith((".yaml", ".yml")):
        yaml.safe_load(pathlib.Path(rel).read_text(encoding="utf-8"))
print("yaml ok")
PY
```

### Targeted tests

Infer targeted tests from the changed area. Examples:

- `gateway/run.py` → relevant gateway tests
- `agent/title_generator.py` → title generator tests
- `plugins/platforms/discord/adapter.py` → Discord adapter tests
- `skills/...` only → skill frontmatter + slash-command scan verification

Prefer focused pytest invocations over the entire suite unless the diff is broad.

Example patterns:

```bash
venv/bin/python -m pytest tests/agent/test_title_generator.py -q -o 'addopts='
venv/bin/python -m pytest tests/gateway/test_discord_thread_auto_title.py -q -o 'addopts='
```

## Publishing

If verification succeeds and the user did **not** request `dry-run` / `no-push`:

```bash
git push origin HEAD:main
```

After push:

```bash
git fetch origin
# keep local main aligned with the published channel when possible
# (do not disturb the user's active source branch)
git branch --set-upstream-to=origin/main main || true
git branch -f main origin/main || true
```

Then remove the temporary worktree unless the user requested `keep-worktree`:

```bash
git worktree remove "$SYNC_DIR"
git branch -D "$SYNC_BRANCH"
```

## Failure handling

### Dirty source branch

If the chosen source branch is dirty, stop and report:

- current branch
- dirty files
- that `/fork-update` will not publish uncommitted work by default
- exact next step: commit or stash, then rerun

### Merge conflict you cannot confidently resolve

If the conflict depends on product intent or would require guessing:

- stop before pushing
- leave the temporary worktree intact
- summarize the conflicted files and tradeoff
- tell the user where the temporary worktree is

### Verification failure

If tests or syntax checks fail:

- do not push
- keep the temporary worktree unless cleanup is explicitly requested
- report the exact failing command and file/test

## Output format for the user

At the end, report briefly:

1. source branch used
2. whether `upstream/main` merged cleanly
3. whether the source branch merged cleanly
4. verification commands run
5. whether push to `origin/main` happened
6. temp worktree path if preserved

Example:

```text
- source branch: local/solstice
- upstream/main merge: clean
- source branch merge: 2 files conflicted, resolved
- verified: git diff --check, py_compile, 2 targeted pytest files
- pushed: yes, origin/main updated
- cleanup: temp worktree removed
```

## Common pitfalls

1. **Running on a dirty source branch and accidentally publishing WIP**
   - Refuse by default. Ask the user to commit/stash first.

2. **Merging directly in the main checkout**
   - Use a temporary worktree so `local/solstice` stays untouched.

3. **Using `upstream/main` as the base instead of `origin/main`**
   - Base on `origin/main`, then merge upstream into it. Otherwise you can accidentally drop already-published fork-specific commits.

4. **Blindly choosing `ours`/`theirs` on conflicts**
   - Preserve intent, not line ownership.

5. **Pushing before verification**
   - Never do it. `git push` is the last step.

6. **Deleting the temporary worktree after a failed verification**
   - Keep it when the failure is unresolved so the user can inspect or resume.
