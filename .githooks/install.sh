#!/bin/sh
# Install this repo's hooks so they cover EVERY worktree.
#
#   sh .githooks/install.sh
#
# Why not `git config core.hooksPath .githooks`, which is the usual advice:
# that path is resolved against each worktree's OWN root. A linked worktree
# checked out at a commit from before .githooks existed finds nothing there and
# runs no hook at all -- silently, with no warning that the guard is off. Three
# of this repo's four worktrees were in exactly that state, including the two
# that other sessions work from.
#
# $GIT_DIR/hooks resolves to the COMMON git dir for every worktree, so a copy
# there is the only placement that actually covers them all.
#
# The trade-off, stated plainly: .git/hooks is not versioned, so this copy can
# drift from .githooks/, which stays the source of truth. Re-run this after
# editing a hook. It is idempotent.

set -e

src_dir=$(cd "$(dirname "$0")" && pwd)
hooks_dir=$(git rev-parse --git-common-dir)/hooks

mkdir -p "$hooks_dir"
n=0
for h in "$src_dir"/*; do
  name=$(basename "$h")
  # install.sh is the installer, not a hook
  [ -f "$h" ] || continue
  [ "$name" = "install.sh" ] && continue
  cp "$h" "$hooks_dir/$name"
  chmod +x "$hooks_dir/$name"
  echo "installed $name -> $hooks_dir/$name"
  n=$((n + 1))
done

# core.hooksPath overrides $GIT_DIR/hooks entirely, so leaving it set would keep
# the per-worktree gap this script exists to close.
if git config --get core.hooksPath >/dev/null 2>&1; then
  git config --unset core.hooksPath
  echo "unset core.hooksPath -- it is what left linked worktrees unguarded"
fi

echo "$n hook(s) now active for every worktree of this repo"
