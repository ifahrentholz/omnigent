# Working on this fork

This fork (`ifahrentholz/omnigent`) is its own version of Omnigent, built
around **multitask orchestration**: parallel sub-agent tasks in their own git
worktrees, a review UI per worktree, and a path back into the orchestrator.
It pulls in [`omnigent-ai/omnigent`](https://github.com/omnigent-ai/omnigent)
regularly but does not send changes back: there are no upstream PRs.

The backlog lives in this fork's issues; the first milestones (M0–M3) are
tracked in the [multitask epic](https://github.com/ifahrentholz/omnigent/issues/18).

## Branches

| Branch | Role |
|---|---|
| `main` | Pure fast-forward mirror of `upstream/main`. Never commit here. |
| `fork/next` | Integration and default branch: every finished fork PR is squash-merged here. |
| `feat/<issue>-<slug>` | One short-lived branch per issue. |

`fork/next` is the fork's GitHub default branch, so clones and the repo page
show the fork's features and `Closes #N` in a merged PR closes the issue.
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

## Fork CI

Pushes and PRs run the full upstream workflow set, about 111 workflows. Public
repositories run on free runners, but the queue gets long with stacked PRs. To
trim it, disable workflows that only make sense for the original project under
**Settings → Actions → Workflows**, for example release, docs-sync, triage and
bot workflows. Do that in the UI, not by editing workflow files, so the fork has
no diff against upstream there.
