---
description: How to compile in SystemVerilog Assertions during simulation with rb test, the Verilator SVA subset, and how firings surface in the results table.
---

# Assertion-Based Verification in Simulation

Pair with [Formal Property Verification](fpv.md) for the proof-engine side. This page is about **simulation** — running `rb test` with assertions compiled into the Verilator-built simulator so SVA properties evaluate on every clock and firings show up in the results table.

> **Status:** Phase 1 ABV. Today this targets Verilator only.

## Enabling assertions

Set `assertions: true` on a test in `tests.yaml`:

```yaml
tests:
  - name: smoke_with_sva
    desc: "smoke test with SVA assertions compiled in"
    reglvl: 0
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    assertions: true
```

When `assertions: true` and the builder is Verilator, `rb test` appends `--assert` and `--coverage-user` to the Verilator compile command. The flags are idempotent — already-configured values in `root_config.yaml` builder opts are not duplicated.

For non-Verilator builders the flag is currently a no-op: VCS/Xcelium SVA enablement is a follow-up.

## What you see in the results table

When at least one test in the run enables `assertions`, `rb test` adds an **Assertions** column:

```text
Test           Result   Description                    Assertions
smoke_with_sva PASS     test passed                    0 fired
sva_violation  FAIL     1 SVA assertion failure(s) …   1 fired
```

- `0 fired` confirms SVA was compiled in and no `%Error: ... Assertion failed` lines were seen.
- `N fired` reports the count; the test is forced to FAIL even if the testbench wrapper printed PASS earlier.

The column is hidden when no test in the run requests assertions, so existing flows are unchanged.

## Verilator SVA subset

Verilator implements a **subset** of IEEE 1800-2017 §16. Today's expectations:

- ✅ Immediate assertions: `assert (cond);`
- ✅ Concurrent assertions on synchronous properties: `always @(posedge clk) assert property (a |-> b);`
- ✅ Cover properties: `cover property (...)` — hits flow into the existing `--coverage-user` pipeline and are merged through `--coverage-merge` like any other user coverage point.
- ⚠️ `disable iff` clauses — not supported.
- ⚠️ Local variables inside properties — not supported.
- ⚠️ Full sequence operators — partial. `##N`, `[*N]`, `|->`, `|=>` work; advanced operators like `intersect`, `throughout`, `within` are not supported.

For a property set that needs the full SVA language, point those properties at `rb fpv` (which can use the slang frontend) or fall back to a commercial simulator. See the [Verilator language support notes](https://verilator.org/guide/latest/languages.html) for the authoritative list.

## How firings are detected

`rb test` parses both `test.log` and `test.err` after simulation looking for lines matching:

```text
%Error: <file>:<line>: Assertion failed in <hier>: '<expr>'
```

A non-zero count flips the result to FAIL and includes the prior result/description in the FAIL description (so a wrapper that printed PASS still surfaces the truth).

## Cover-property hits

Cover properties land in the same `coverage.dat` Verilator emits today, so:

- `rb -M cov test ... ` continues to be the canonical path for full coverage HTML / Coverview packaging.
- With just `assertions: true` (no `-M cov`), `coverage.dat` still exists per-run because `--coverage-user` was injected — but only the user-coverage type is present. Merge with `--coverage-merge` to roll up.

See [Coverage](coverage.md) for the merge pipeline and the
[Verilator coverage analysis note](https://github.com/rtl-buddy/rtl_buddy/blob/main/src/rtl_buddy/tools/verilator_cov_analysis.md)
for how the raw simulator coverage points relate to LCOV outputs.

## Relationship to `rb fpv`

`rb fpv` proves assertions exhaustively up to a bound; `rb test` with `assertions: true` exercises them on the dynamic stimulus your testbench drives. The two are complementary:

- Use simulation to find **the obvious bugs cheaply** — every existing test now polices SVA properties as a side effect of running.
- Use [`rb fpv`](fpv.md) to **prove invariants** over all reachable behaviors up to the bound.

A property that proves bounded under `rb fpv` and never fires in simulation is the strongest signal you'll get without a commercial completeness tool.
