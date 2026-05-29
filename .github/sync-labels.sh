#!/usr/bin/env bash
# Sync the shared label taxonomy (.github/labels.json) across all rtl-buddy repos.
#
# This is a MANUAL step — run it locally with your own `gh` auth. Your local
# token already has write access to every rtl-buddy repo, so no CI secret is
# needed. Run it after editing labels.json (and commit the change).
#
# Usage:
#   .github/sync-labels.sh             # upsert all labels into all repos (idempotent)
#   .github/sync-labels.sh --dry-run   # show what would change, make no edits
#   .github/sync-labels.sh --prune     # also delete managed labels absent from labels.json
#                                       # (scoped to area/*, version/*, discussion only)
set -euo pipefail

OWNER=rtl-buddy
REPOS=(rtl_buddy rtl-buddy-cdc rtl-buddy-view rtl-buddy-xeno rtl-buddy-axi-profiler)

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILE="$DIR/labels.json"

DRY=false; PRUNE=false
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=true ;;
    --prune)   PRUNE=true ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "run 'gh auth login' first" >&2; exit 1; }

for repo in "${REPOS[@]}"; do
  target="$OWNER/$repo"
  echo "== $target =="

  # upsert (create-or-update) every label from the source of truth
  jq -c '.[]' "$FILE" | while read -r row; do
    name=$(jq -r '.name'        <<<"$row")
    color=$(jq -r '.color'      <<<"$row")
    desc=$(jq -r '.description' <<<"$row")
    if $DRY; then
      echo "  would upsert: $name"
    else
      gh label create "$name" --repo "$target" --color "$color" --description "$desc" --force >/dev/null
      echo "  upserted: $name"
    fi
  done

  # optional prune: delete managed-namespace labels no longer in labels.json
  if $PRUNE; then
    comm -23 \
      <(gh label list --repo "$target" --limit 200 --json name --jq '.[].name' \
          | grep -E '^(area/|version/|discussion$)' | sort) \
      <(jq -r '.[].name' "$FILE" | sort) \
    | while read -r stale; do
        [ -z "$stale" ] && continue
        if $DRY; then echo "  would delete: $stale"
        else gh label delete "$stale" --repo "$target" --yes; echo "  deleted: $stale"; fi
      done
  fi
done

echo "done"
