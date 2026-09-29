#!/usr/bin/env bash
# Sync the label taxonomy in .github/labels.json to all rtl-buddy repos.
# Run manually after editing labels.json; uses your own `gh` auth.
#
#   .github/sync-labels.sh             # create or update all labels
#   .github/sync-labels.sh --dry-run   # show changes, make none
#   .github/sync-labels.sh --prune     # also delete area/*, version/* and discussion
#                                      # labels that are not in labels.json
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
