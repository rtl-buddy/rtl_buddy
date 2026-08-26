---
description: Enable SystemVerilog assertions in Verilator simulation, interpret assertion failures, and collect cover-property hits.
---

# Assertion-based verification in simulation

Set `assertions: true` on a test to compile SystemVerilog assertions into a Verilator simulation. Assertion firings then affect the test verdict and appear in results.

## Enable assertions

```yaml
tests:
  - name: smoke_with_sva
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    assertions: true
```

For Verilator, rtl_buddy adds `--assert` and `--coverage-user` unless the builder options already contain them. For other builders, the setting has no effect and logs `compile.assertions_not_verilator` at WARNING.

Verilator supports immediate assertions, common synchronous concurrent assertions, cover properties, and some sequence operators. It does not implement the full IEEE 1800 assertion language. Check the [Verilator language support](https://verilator.org/guide/latest/languages.html) for the installed version before relying on constructs such as `disable iff`, local property variables, or advanced sequence operators. Use [`rb fpv`](fpv.md) with the slang frontend or a simulator with the required SVA support when Verilator cannot compile the property set.

## Interpret assertion results

When any selected test enables assertions, `rb test` and `rb regression` add an **Assertions** column:

```text
Test           Result   Description                    Assertions
smoke_with_sva PASS     test passed                    0 fired
sva_violation  FAIL     1 SVA assertion failure(s) …   1 fired
```

- `0 fired` means assertions were enabled and no matching failure was recorded.
- `N fired` forces the test to FAIL, even if the testbench reported PASS first.

rtl_buddy scans `test.log` and `test.err` for Verilator assertion errors, including lines prefixed by a simulation timestamp:

```text
[500] %Error: tb_top.sv:32: Assertion failed in top.dut: 'assert' failed.
```

The Assertions column is omitted when no selected test enables assertions.

## Collect cover-property hits

`assertions: true` also enables Verilator user coverage, so labeled `cover property` hits appear in each run's `coverage.dat`. Use the normal coverage flags to merge or report them:

```bash
rb -M cov test smoke_with_sva --coverage-merge
```

Under `--machine`, cover points are reported by name per test and summed across the run. See [Coverage](coverage.md#inspect-cover-property-hits) for the data and merge behavior.

Simulation checks only the stimulus that ran. Use [Formal Property Verification](fpv.md) when the property needs bounded proof over all modeled behaviors.
