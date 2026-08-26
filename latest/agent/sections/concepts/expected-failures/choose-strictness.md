## Choose strictness

| Marker | Actual failure | Unexpected pass | Use when |
| --- | --- | --- | --- |
| `xfail: true` | `XFAIL`, counts as pass | `XPASS`, counts as pass | Either outcome is acceptable |
| `xfail_strict: true` | `XFAIL`, counts as pass | `XPASS`, counts as fail | A pass means the marker is stale |

If both fields are set, strict behavior wins. `SKIP` and `NA` are unchanged.

Prefer `xfail_strict: true` for a known bug or intentionally failing teaching case so the regression reports when the underlying behavior changes.
