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

Run the CLI from inside the git repository foreman should work on:

```bash
omnigent run path/to/examples/foreman
```

To start it from the web UI instead, register foreman as a built-in agent
when the server starts, then pick **foreman** under New chat with the
repository as workspace:

```bash
OMNIGENT_BUILTIN_AGENT_DIRS="$PWD/examples/foreman" omnigent server
```

In a dev checkout, prefix `just dev` the same way.

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

Each worktree also gets its own port range for dev servers (`$PORT`,
`$OMNIGENT_PORT_BASE`), shown next to the task in the Worktrees view. See
[Dev server ports](../../docs/AGENT_YAML_SPEC.md#dev-server-ports).

## Landing a task

Tasks that change the same files get a badge in the Worktrees view. A
`git merge-tree` dry-run tells a clean overlap ("2 shared") from a real
conflict ("conflict", or "conflicts with main" once another task landed). It
covers committed work only. Expand the task to see the conflicting files.

Two conflicting tasks can still both land, one after the other:

- **Land…** one of them. The confirmation offers "Then ask *<other task>* to
  update from main" (on by default), which asks each conflicting worker to
  merge `main` into its branch and resolve the conflicts.
- A task that already conflicts with its base shows **Resolve…**. Add an
  optional note ("keep both", "main wins") and the worker merges the base,
  resolves the conflicts, reruns the tests and commits. It asks back when the
  two changes contradict each other and the note does not settle it.
- **Tell orchestrator** asks foreman which task should land first.

foreman never merges on its own. In the Worktrees view, expand a task and
choose one of:

- **Land…**: merge the branch into its base in your checkout, as a merge
  commit or squashed. Both sides must be clean. A conflicting merge is
  aborted and lists the files.
- **Ask for PR**: the worker pushes its branch and opens a pull request. Deleting the
foreman session with "delete branch" also removes every task's worktree and
branch.
