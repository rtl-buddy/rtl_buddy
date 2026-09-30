#!/usr/bin/env bash
# Diff `_tokenize` and `_extract_names` in src/rtl_buddy/constraints/tcl_tokenizer.py
# against rtl-buddy-cdc at UPSTREAM_REF. Exits non-zero on drift.
# Fix drift by re-copying from upstream, then bump UPSTREAM_REF here and in the
# vendored file's header. CI runs this as advisory (continue-on-error).
#
# Usage:  bash scripts/check-vendored-tokenizer.sh
# Needs:  gh (authenticated), python3

set -euo pipefail

UPSTREAM_REPO="rtl-buddy/rtl-buddy-cdc"
UPSTREAM_PATH="src/rtl_buddy_cdc/tcl_tokenizer.py"
UPSTREAM_REF="df996d55af0e8b824183d03d531426b8601af429"
FUNCS="_tokenize _extract_names"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDORED="${repo_root}/src/rtl_buddy/constraints/tcl_tokenizer.py"

if [[ ! -f "${VENDORED}" ]]; then
  echo "check-vendored-tokenizer: ${VENDORED} not found" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

echo "Fetching ${UPSTREAM_REPO}/${UPSTREAM_PATH} @ ${UPSTREAM_REF}"
if ! gh api \
  -H "Accept: application/vnd.github.raw" \
  "repos/${UPSTREAM_REPO}/contents/${UPSTREAM_PATH}?ref=${UPSTREAM_REF}" \
  >"${tmpdir}/upstream.py"; then
  echo "check-vendored-tokenizer: could not fetch upstream (auth/network?)" >&2
  exit 1
fi

extract() {
  # $1 = source file, $2 = output file
  python3 - "$1" "$2" "${FUNCS}" <<'PY'
import ast
import sys

src_path, out_path, names = sys.argv[1], sys.argv[2], sys.argv[3].split()
source = open(src_path, encoding="utf-8").read()
lines = source.splitlines(keepends=True)
found = {}
for node in ast.parse(source).body:
    if isinstance(node, ast.FunctionDef) and node.name in names:
        found[node.name] = "".join(lines[node.lineno - 1 : node.end_lineno])
missing = [n for n in names if n not in found]
if missing:
    sys.exit(f"{src_path}: function(s) not found at module level: {', '.join(missing)}")
with open(out_path, "w", encoding="utf-8") as fh:
    for name in names:
        fh.write(found[name])
        fh.write("\n")
PY
}

extract "${tmpdir}/upstream.py" "${tmpdir}/upstream.funcs"
extract "${VENDORED}" "${tmpdir}/vendored.funcs"

if diff -u "${tmpdir}/upstream.funcs" "${tmpdir}/vendored.funcs" \
  --label "upstream ${UPSTREAM_REPO}@${UPSTREAM_REF}:${UPSTREAM_PATH}" \
  --label "vendored src/rtl_buddy/constraints/tcl_tokenizer.py"; then
  echo "OK: vendored tokenizer matches ${UPSTREAM_REPO}@${UPSTREAM_REF}"
  exit 0
fi

cat >&2 <<EOF

check-vendored-tokenizer: the vendored copy has DRIFTED from upstream.

Fix it by re-copying the functions from upstream (do not hand-edit the
vendored bodies), then update UPSTREAM_REF in this script and the pinned
commit in src/rtl_buddy/constraints/tcl_tokenizer.py's header.
EOF
exit 1
