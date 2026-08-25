---
name: rtl-buddy-fpv
description: Run and review rtl_buddy formal verification; use for UNKNOWN results, frontend limits, vacuity, COI, and mutation guardrails.
---

# rtl_buddy formal verification

Report `rb --version` at the top of every run summary.

Use `rb --machine`; read `rb fpv --help`, `rb fpv-regression --help`, and
`rb --machine docs show concepts/fpv` for configs and worked procedures.

## Run and interpret

- Gate the environment with `rb --machine tool-check --required-for fpv`.
- Treat `artefacts/<run>/sby_workdir/status` as the formal verdict when present,
  and read each machine result's `vacuity` and `coi` blocks.
- A PASS with unreachable covers, vacuous properties, or dead assumptions is a
  false green. Report those guardrails with the result.
- `UNKNOWN` in `mode: prove` can mean the property is true but non-inductive.
  Strengthen the invariant or exclude unreachable predecessor states before
  merely increasing depth.

## Authoring guardrails

- Probe the installed slang/Yosys build's supported SVA constructs; support can
  vary by build even when the nominal version matches.
- Constrain reset/initial state so proof does not begin in unreachable state.
- Confirm every intended cover is reachable.
- Make one deliberate mutation and confirm the expected assertion fails before
  reporting a proof environment as trustworthy.
- Track intentionally non-inductive or unsupported cases with the documented
  `xfail`/`xfail_strict` semantics.

For mutation campaigns, use `rb --machine docs show concepts/mut`. Survivors are
verification holes; mutants that cannot build are errors, not kills.
