## Choose strictness

| Marker | Actual failure | Unexpected pass | Use when |
| --- | --- | --- | --- |
| `xfail: true` | `XFAIL`, counts as pass | `XPASS`, counts as pass | Either outcome is acceptable |
| `xfail_strict: true` | `XFAIL`, counts as pass | `XPASS`, counts as fail | A pass means the marker is stale |

- If both are set, strict wins.
- `SKIP` and `NA` are unchanged. A marker does not cover an unknown `NA`, which still exits 1.
- Prefer `xfail_strict: true` for a known bug or an intentionally failing teaching case, so the regression reports when the behavior changes.
