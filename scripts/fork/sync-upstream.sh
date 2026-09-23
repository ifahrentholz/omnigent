#!/usr/bin/env bash
# Fast-forward the fork's `main` to omnigent-ai/omnigent `main`, then report
# how far `fork/next` (the integration branch) is behind it.
#
# Usage: scripts/fork/sync-upstream.sh [--rebase-next]
#   --rebase-next  also rebase fork/next onto the new main and force-push it
#                  (fork/next only holds squash-merged fork PRs).
set -euo pipefail

upstream_url="https://github.com/omnigent-ai/omnigent.git"
git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$upstream_url"
git fetch --quiet upstream main
git fetch --quiet origin

# main must stay a pure mirror: refuse anything but a fast-forward.
if ! git merge-base --is-ancestor origin/main upstream/main; then
  echo "origin/main has commits upstream/main lacks; refusing to overwrite." >&2
  exit 1
fi
git push --quiet origin upstream/main:main
echo "main -> $(git rev-parse --short upstream/main)"

behind=$(git rev-list --count origin/fork/next..upstream/main)
echo "fork/next is ${behind} commit(s) behind upstream/main"

if [[ "${1:-}" == "--rebase-next" && "$behind" -gt 0 ]]; then
  git checkout --quiet -B fork/next origin/fork/next
  git rebase upstream/main
  git push --force-with-lease origin fork/next
  echo "fork/next rebased onto upstream/main"
fi
