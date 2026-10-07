---
name: rtl-buddy-fpv
description: Run and review rtl_buddy formal verification; use for UNKNOWN results, frontend limits, vacuity, COI, and mutation guardrails.
---

# rtl_buddy formal verification

Report `rb --version` at the top of every run summary.

Use `rb --machine`. For configs and worked procedures read `rb fpv --help`, `rb fpv-regression --help` and `rb --machine docs show concepts/fpv`.

## Run and interpret

- Gate the environment with `rb --machine tool-check --required-for fpv`.
- `fpv` and `fpv-regression` exit 0 when every result counts as successful (`PASS`, `SKIP`, `XFAIL`, non-strict `XPASS`), 1 for any `FAIL` or strict `XPASS`, and 2 for a fatal configuration or environment error.
- `artefacts/<run>/sby_workdir/status` is the formal verdict when present. Read each machine result's `vacuity` and `coi` blocks too.
- A PASS with unreachable covers, vacuous properties or dead assumptions is a false green. Report those guardrails with the result.
- The vacuity pass runs as an sby cover task on `vacuity_engines:`, which defaults to the `smtbmc` entries of `engines:`, else `smtbmc yices`. An `abc pdr` proof keeps its vacuity check without `vacuity: false`.
- `UNKNOWN` in `mode: prove` can mean the property is true but not inductive. Strengthen the invariant or exclude unreachable predecessor states before raising depth.

## Authoring guardrails

- Probe the installed slang/Yosys build's supported SVA constructs; support varies by build even at the same nominal version.
- Constrain reset and initial state so the proof does not start in an unreachable state.
- Confirm every intended cover is reachable.
- Make one deliberate mutation and confirm the expected assertion fails before calling a proof environment trustworthy.
- Track intentionally non-inductive cases with `xfail`/`xfail_strict`. They excuse a property sby disproved, not an `UNKNOWN`, a solver timeout or an sby error, which stay `FAIL`.

## Mutation campaigns

Use `rb --machine docs show concepts/mut`. Survivors are verification holes; mutants that cannot build are errors, not kills. `mut run` exits 0 when the campaign is scorable and 1 when nothing is scorable; score and survivor count do not gate it. Fatal errors exit 2.
