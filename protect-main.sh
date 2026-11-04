#!/usr/bin/env bash
#
# Protect this repo's main branch on GitHub with .github/rulesets/main.json:
#   - changes reach main through pull requests (no approval needed, so it works solo)
#   - the `checks-passed` job in .github/workflows/test.yml (lint + every test job) must pass
#   - main can't be force-pushed or deleted
# No one bypasses it, admins included. Turn it off under Settings -> Rules if you must.
#
#   ./protect-main.sh                 # the GitHub repo this folder pushes to
#   ./protect-main.sh owner/repo
#
# Safe to re-run: it updates the ruleset when one with the same name exists.
# Needs the GitHub CLI signed in as a repo admin. Private repos need GitHub Pro or a paid org plan.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null || die "the GitHub CLI (gh) isn't installed: https://cli.github.com"
ruleset=.github/rulesets/main.json
name="$(sed -nE 's/^  "name": "([^"]+)".*/\1/p' "$ruleset" | head -1)"
[ -n "$name" ] || die "couldn't read the ruleset name from $ruleset"
repo="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"

id="$(gh api "repos/$repo/rulesets" --jq ".[] | select(.name == \"$name\") | .id")"
if [ -n "$id" ]; then
  gh api -X PUT "repos/$repo/rulesets/$id" --input "$ruleset" >/dev/null
  say "updated the \"$name\" ruleset on $repo"
else
  gh api -X POST "repos/$repo/rulesets" --input "$ruleset" >/dev/null
  say "created the \"$name\" ruleset on $repo"
fi
echo "    https://github.com/$repo/rules"
