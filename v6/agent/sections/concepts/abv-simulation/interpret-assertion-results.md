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
