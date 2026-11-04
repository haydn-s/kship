#!/usr/bin/env bash
#
# Publish this folder to a new, EMPTY GitHub repository you've created
# (no README, license or .gitignore added on GitHub's side).
#
#   ./publish.sh git@github.com:haydn-s/kship.git
#   ./publish.sh https://github.com/haydn-s/kship
#
# It points kship at that repo (CI workflows install kship from it), makes the
# first commit, tags v<version>, pushes both, protects main (protect-main.sh),
# and reinstalls your local kship so `kship ci` picks up the new URL. Run it once.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

remote="${1:-}"
[ -n "$remote" ] || die "usage: ./publish.sh <github repo url>   e.g. git@github.com:haydn-s/kship.git"
[ -d .git ] && die "this folder is already a git repository; push it with git directly"
command -v git >/dev/null || die "git isn't installed"
git config user.name >/dev/null && git config user.email >/dev/null \
  || die "set your git identity first: git config --global user.name ... / user.email ..."

slug="$(printf '%s' "$remote" | sed -E 's#^(git@github\.com:|ssh://git@github\.com/|https://github\.com/)##; s#\.git$##; s#/$##')"
case "$slug" in
  */*) ;;
  *) die "couldn't read owner/repo from '$remote'" ;;
esac
web="https://github.com/$slug"
version="$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -1)"

# 1. Point kship's own references at the new repo.
if [ "$slug" != "haydn-s/kship" ]; then
  say "pointing kship at $web"
  perl -pi -e "s#https://github\.com/haydn-s/kship#$web#g" pyproject.toml README.md src/kship/cli.py
fi

# 2. First commit and tag.
say "creating the first commit"
git init -q -b main
git add -A
git commit -q -F - <<EOF
kship $version: validate, gate and ship Kaggle submissions

CLI and GitHub Actions workflow for Kaggle competitions: validates a
submission against the sample, dedupes, gates on CV, keeps a reserve of
daily submissions, uploads, and records the leaderboard score with the
commit that produced it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01YEfN8DpGLToiJVGgrDTnnk
EOF
git tag "v$version"

# 3. Push.
say "pushing to $remote"
git remote add origin "$remote"
git push -u origin main
git push origin "v$version"

# 4. Protect main: pull requests only, lint and tests must pass.
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  say "protecting main"
  ./protect-main.sh "$slug" || say "main isn't protected yet (see the error above); run ./protect-main.sh once that's fixed"
else
  say "main isn't protected yet: install and sign in to the GitHub CLI (gh), then run ./protect-main.sh"
fi

# 5. Reinstall the local tool so its metadata carries the new repo URL.
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^kship '; then
  say "reinstalling kship so \`kship ci\` points new projects at $web"
  uv tool install --force --editable . >/dev/null
elif command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q '^kship '; then
  pipx install --force --editable . >/dev/null
fi

echo
say "published: $web  (tag v$version)"
echo "    competition projects' CI will install: git+$web@v$version"
echo "    next: kship new <competition-slug>"
