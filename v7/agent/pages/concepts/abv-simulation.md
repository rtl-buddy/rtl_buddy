---
description: Enable SystemVerilog assertions in Verilator simulation, interpret assertion failures, and collect cover-property hits.
---

# Assertion-based verification in simulation

Set `assertions: true` on a test to compile SystemVerilog assertions into a Verilator simulation. Assertion firings then change the test verdict and appear in the results.

## Enable assertions

```yaml
tests:
  - name: smoke_with_sva
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    assertions: true
```

For Verilator, rtl_buddy adds `--assert` and `--coverage-user` unless the builder options already include them. For other builders the setting has no effect, and rtl_buddy logs `compile.assertions_not_verilator` at WARNING.

Verilator supports immediate assertions, common synchronous concurrent assertions, cover properties, and some sequence operators, but not the full IEEE 1800 assertion language. Check the [Verilator language support](https://verilator.org/guide/latest/languages.html) for your version before using `disable iff`, local property variables, or advanced sequence operators. If Verilator cannot compile the properties, use [`rb fpv`](fpv.md) with the slang frontend, or a simulator with the needed SVA support.

## Interpret assertion results

When any selected test enables assertions, `rb test` and `rb regression` add an **Assertions** column. It is omitted otherwise.

```text
Test           Result   Description                    Assertions
smoke_with_sva PASS     test passed                    0 fired
sva_violation  FAIL     1 SVA assertion failure(s) …   1 fired
```

- `0 fired`: assertions were enabled and no failure was recorded.
- `N fired`: the test is forced to FAIL, even if the testbench reported PASS first.

rtl_buddy finds failures by scanning `test.log` and `test.err` for Verilator assertion errors, including lines prefixed with a simulation timestamp:

```text
[500] %Error: tb_top.sv:32: Assertion failed in top.dut: 'assert' failed.
```

## Collect cover-property hits

`assertions: true` also enables Verilator user coverage, so labeled `cover property` hits appear in each run's `coverage.dat`. Merge or report them with the normal coverage flags:

```bash
rb -M cov test smoke_with_sva --coverage-merge
```

Under `--machine`, cover points are reported by name per test and summed over the run. See [Coverage](coverage.md#inspect-cover-property-hits).

Simulation checks only the stimulus that ran. When a property needs a bounded proof over all modeled behavior, use [Formal Property Verification](fpv.md).
