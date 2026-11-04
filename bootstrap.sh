#!/usr/bin/env bash
#
# One-time Kaggle setup for this machine:
#   1. installs the `kaggle` CLI and `kship` as global tools (not tied to any project venv)
#   2. gets you signed in to Kaggle if you aren't already
#   3. runs `kship doctor` to prove it all works
#
# Safe to re-run.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }

# -- 1. tools ------------------------------------------------------------------
# kaggle 2.2.x imports `six` without declaring it, so a clean install crashes on
# launch unless six is installed alongside it.

if ! command -v uv >/dev/null 2>&1 && ! command -v pipx >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "installing uv with Homebrew"
    brew install uv
  else
    warn "needs uv (or pipx). Install uv with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
  fi
fi

if command -v uv >/dev/null 2>&1; then
  say "installing the kaggle CLI (global, via uv)"
  uv tool install --force kaggle --with six
  say "installing kship from $HERE (editable: edits to the source take effect immediately)"
  uv tool install --force --editable "$HERE"
  uv tool update-shell >/dev/null 2>&1 || true
else
  say "installing the kaggle CLI (global, via pipx)"
  pipx install --force kaggle
  pipx inject kaggle six
  say "installing kship from $HERE"
  pipx install --force --editable "$HERE"
  pipx ensurepath >/dev/null 2>&1 || true
fi

export PATH="$HOME/.local/bin:$PATH"

# -- 2. credentials --------------------------------------------------------------

have_creds() {
  [ -n "${KAGGLE_API_TOKEN:-}" ] && return 0
  [ -n "${KAGGLE_USERNAME:-}" ] && [ -n "${KAGGLE_KEY:-}" ] && return 0
  for f in access_token access_token.txt kaggle.json credentials.json; do
    [ -s "$HOME/.kaggle/$f" ] && return 0
  done
  return 1
}

if have_creds; then
  say "Kaggle credentials already present"
else
  mkdir -p "$HOME/.kaggle" && chmod 700 "$HOME/.kaggle"
  echo
  echo "Sign in to Kaggle. Two options:"
  echo "  token    paste a token from https://www.kaggle.com/settings/api (\"Generate New Token\")."
  echo "           Best if you'll use CI: the same token becomes the repo secret in one command."
  echo "  browser  press Enter to sign in through the browser (nothing to copy)."
  echo
  printf 'Paste a KGAT_... token, or press Enter for browser sign-in: '
  read -rs token || token=""
  echo
  if [ -n "$token" ]; then
    umask 077
    printf '%s' "$token" > "$HOME/.kaggle/access_token"
    chmod 600 "$HOME/.kaggle/access_token"
    unset token
    say "saved ~/.kaggle/access_token (readable only by you)"
  else
    kaggle auth login
  fi
fi

# -- 3. verify -------------------------------------------------------------------

echo
kship doctor
echo
say "done. Start a competition with:  kship new <competition-slug>"
