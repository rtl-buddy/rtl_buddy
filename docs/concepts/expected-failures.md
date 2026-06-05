---
description: How rtl_buddy's xfail / xfail_strict markers re-interpret PASS / FAIL / SKIP verdicts across fpv, tests, synth, cdc, pnr, and power runs.
---

# Expected failures (xfail)

Mark a verification that is **known not to hold** — a teaching property
that is true but not inductive under FPV `mode: prove`, a test guarding
an unfixed bug, a CDC lint with intentional violations, a synthesis or
P&R configuration whose current failure is documented — when you want it
tracked in the suite rather than deleted.

`xfail` and `xfail_strict` are one shared mechanism wired across every
command whose results carry a PASS / FAIL / SKIP verdict: `fpv.yaml`,
`tests.yaml`, `synth.yaml`, `cdc.yaml`, `pnr.yaml`, and `power.yaml`. A
verification is treated as expected-to-fail when **either** marker is
set; the verdict is then re-interpreted:

| Actual outcome | Reported as | Counts as |
|---|---|---|
| FAIL | `XFAIL` | **pass** — the expected failure happened, so it does not fail the run or the regression |
| PASS | `XPASS` | depends on strictness (see below) |
| SKIP / NA | unchanged | unchanged |

## Strict vs non-strict

The two markers differ **only** in how an unexpected pass is counted:

| Marker | `XPASS` counts as | Use when |
|---|---|---|
| `xfail: true` | **pass** (non-strict) | the run *may* start passing and that is fine / not worth failing on |
| `xfail_strict: true` | **fail** (strict) | a pass means the marker is stale and you want to be told — the safe choice for a regression guard |

If both are set, strict wins. Each verification picks the marker it
needs. A common pattern: `xfail_strict: true` on a teaching demo or a
not-yet-fixed bug, so the regression turns red (via `XPASS`) the moment
the run starts passing and the marker should be removed.

## Caveat

Like pytest `xfail` without `raises=`, this does not distinguish a
genuine disproof from an infrastructure error that also surfaces as a
FAIL. Reserve the marker for runs whose failure mode is understood.

## Where it shows up

The marker is a per-verification field in each command's yaml; the
exact schema entries live in [YAML Formats](../reference/yaml.md).
Common contexts:

- **`fpv.yaml`** — a teaching property that is true but not inductive
  under `mode: prove` (see [Writing properties that prove](fpv.md#writing-properties-that-prove-bmc-vs-induction)).
- **`tests.yaml`** — a test that guards a known unfixed bug or an
  environment limitation.
- **`cdc.yaml`** — a design with known / intentional CDC violations
  tracked in the suite.
- **`synth.yaml` / `pnr.yaml` / `power.yaml`** — a configuration whose
  current failure is documented and tracked.
