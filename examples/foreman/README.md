# foreman: parallel tasks, one git worktree each

`foreman` turns every request into a task for a coding sub-agent and hands it
off immediately. Each task runs in its **own git worktree on its own branch**,
so several tasks progress in parallel without touching each other's files, and
foreman is free for your next request right away.

```
you: "Please fix the login empty-password bug"
foreman: dispatched `login-empty-password` to claude_code   ← turn ends
you: "Also add rate limiting to /api/search"
foreman: dispatched `search-rate-limit` to claude_code      ← runs in parallel
…
foreman: login-empty-password done — branch omni/login-empty-password-3f9a1c, tests green
```

## Run it

Start foreman in a git repository on a connected host, from the web UI
(New session → pick the repo as workspace) or the CLI:

```bash
omnigent run examples/foreman
```

Workers are Claude Code (`claude-native`, the default) and Codex
(`codex-native`, ask for it: "…with codex"). Both CLIs must be installed on the
host.

## What happens

- **Worktree per task.** The workers declare `worktree: true`. Each dispatch
  creates `<repo>-worktrees/omni-<title>-<id>` on a fresh `omni/<title>-<id>`
  branch, forked from the branch foreman's workspace has checked out. The
  worker's terminal and file panel live in that worktree.
- **Non-blocking.** foreman dispatches with `sys_session_send`, confirms in one
  line and ends its turn. Results arrive in its inbox and wake it for a short
  summary.
- **Bounded.** `max_running_subagents: 4` caps concurrent tasks; a fifth
  request waits until a worker finishes.

## Review and annotate

Open the right rail → **Agents** → **Worktrees** view. Every task shows
`branch → base`, `+/−` totals and its changed files, including committed work.
Click a file to open it in the task's branch diff. Add comments on lines, then:

- **Address All** sends them to the worker directly, or
- **To orchestrator** sends them to foreman, which routes them to the right
  worker.

The worker addresses the comments in its worktree and commits again.

## Preparing worktrees

If tasks need git-ignored files (`.env`) or installed dependencies, commit a
`.omnigent/worktree.yaml` to your repository. See
[Preparing worktrees](../../docs/AGENT_YAML_SPEC.md#preparing-worktrees-omnigentworktreeyaml).

## Landing a task

foreman never merges on its own. In the Worktrees view, expand a task and
choose one of:

- **Land…**: merge the branch into its base in your checkout, as a merge
  commit or squashed. Both sides must be clean. A conflicting merge is
  aborted and lists the files.
- **Ask for PR**: the worker pushes its branch and opens a pull request. Deleting the
foreman session with "delete branch" also removes every task's worktree and
branch.
