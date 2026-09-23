# Working on this fork

This fork (`ifahrentholz/omnigent`) develops **multitask orchestration**:
parallel sub-agent tasks in their own git worktrees, a review UI per worktree,
and a path back into the orchestrator. Finished pieces are proposed upstream to
[`omnigent-ai/omnigent`](https://github.com/omnigent-ai/omnigent), one issue per
PR. This file is fork-only and never goes upstream.

The backlog is the [epic](https://github.com/ifahrentholz/omnigent/issues/18),
with milestones M0–M3 and the `upstream-candidate` label.

## Branches

| Branch | Role |
|---|---|
| `main` | Pure fast-forward mirror of `upstream/main`. Never commit here. |
| `fork/next` | Integration branch: every finished fork PR is squash-merged here. |
| `feat/<issue>-<slug>` | One short-lived branch per issue. |

Fork PRs target `fork/next`. Dependent work may stack: open the PR against the
previous feature branch, then retarget it to `fork/next` once that one lands. A
rebase whose tree is identical to the CI-tested head does not need a new CI run.

Sync with upstream:

```bash
scripts/fork/sync-upstream.sh              # fast-forward main, show the drift
scripts/fork/sync-upstream.sh --rebase-next # also rebase fork/next onto it
```

## Keeping rebases cheap

Upstream moves fast (tens of commits a day). Prefer new modules over edits to
hot files. The fork's features mostly live in:

- `omnigent/runner/subagent_worktree.py`, `subagent_cap.py`, `branch_diff.py`,
  `branch_merge.py`
- `omnigent/host/worktree_setup.py`
- `web/src/hooks/useBranchDiff.ts`, `useLandBranch.ts`
- `web/src/shell/WorktreesPanel.tsx`, `DiffSourceToggle.tsx`
- `examples/foreman/`

Edits to shared files (`tool_dispatch.py`, `orchestration.py`, `FileViewer.tsx`
and similar) stay small and are listed in each PR.

## Checks before a PR

Follow upstream's `CONTRIBUTING.md` / `AGENTS.md`:

```bash
uv sync --extra all --group dev
uv run --no-sync pytest tests/<area> -n 8
uv run --no-sync pre-commit run --files $(git diff --name-only fork/next)
uv run --no-sync python scripts/dump_openapi.py --check
(cd web && pnpm run lint && pnpm exec tsc -b && pnpm exec vitest run)
uv run --no-sync pytest tests/e2e/test_subagent_worktree_e2e.py   # mock LLM, no creds
```

- Sign off every commit (`git commit -s`, DCO).
- New user-facing behavior needs an e2e happy path (`tests/e2e/`). UI changes
  need a Playwright test (`tests/e2e_ui/`).
- UI changes that alter a Storybook story need new visual baselines. Run
  **UI Snapshot** via `workflow_dispatch` on the branch (or
  `tests/e2e_ui/visual/regen_baseline_docker.sh`) and commit the artifact.

### Known noise on this machine and in fork CI

- **macOS-only failures** (they also fail on untouched `upstream/main`):
  `linux_bwrap` sandbox tests, tmux socket path length, a local Claude
  subscription changing model catalogs, `psycopg` not installed, and the
  account-revocation environment tests.
- **Fork CI gates that cannot pass here:** Maintainer Approval, E2E UI
  Required, Resolve latest stable tag (the fork has no release tags).
- **Flaky:** `tests/e2e/test_repl_sessions_approval_e2e.py` (pexpect timeouts).
  It passes locally.
- **Cancelled runs** after a force-push show up as failed checks with
  unexpanded `${{ matrix.* }}` names. The newer run on the same head counts.

## Going upstream

Upstream requires an **upstream issue** for every PR (`Closes
omnigent-ai/omnigent#…`); fork issues don't count. Migrations (e.g. #3) need
code-owner approval there. Suggested order, each as its own PR cherry-picked
from its `fork/next` squash commit onto `upstream/main`:

1. #2 worktree per sub-agent, then #3 base branch, #6 title race, #7 running cap
2. #4 child summary fields, #8 branch-diff endpoints, #25 file-panel root
3. #9 branch baseline UI, #10 Worktrees view, #11 comments to orchestrator
4. #5 worktree setup + cascade cleanup, #14 land a branch, #13 foreman example

Before the first one, ask upstream (Discord or an issue) whether sub-agent
worktree isolation is already in flight. The code references design docs such
as `designs/SESSION_GIT_WORKTREE.md` and `designs/STEERABLE_SUBAGENTS.md` that
are not public.

## Fork CI

Pushes and PRs run the full upstream workflow set, about 111 workflows. Public
repositories run on free runners, but the queue gets long with stacked PRs. To
trim it, disable workflows that only make sense upstream under
**Settings → Actions → Workflows**, for example release, docs-sync, triage and
bot workflows. Do that in the UI, not by editing workflow files, so the fork has
no diff against upstream there.
