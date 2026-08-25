---
description: Mark known failures and choose whether an unexpected pass should fail a test, formal, synthesis, P&R, or power run.
---

# Expected Failures

Use an expected-failure marker only for a known, understood failure that should remain visible in a suite. The fields are available on runs in `tests.yaml`, `fpv.yaml`, `synth.yaml`, `pnr.yaml`, and `power.yaml`.

## Choose strictness

| Marker | Actual failure | Unexpected pass | Use when |
| --- | --- | --- | --- |
| `xfail: true` | `XFAIL`, counts as pass | `XPASS`, counts as pass | Either outcome is acceptable |
| `xfail_strict: true` | `XFAIL`, counts as pass | `XPASS`, counts as fail | A pass means the marker is stale |

If both fields are set, strict behavior wins. `SKIP` and `NA` are unchanged.

Prefer `xfail_strict: true` for a known bug or intentionally failing teaching case so the regression reports when the underlying behavior changes.

## Interpret failures carefully

The marker remaps any `FAIL`; it does not distinguish the expected design failure from a compile, tool, or environment failure. Confirm the failure reason before accepting `XFAIL`.

See [YAML Formats](../reference/yaml.md) for the field on each configuration type.
