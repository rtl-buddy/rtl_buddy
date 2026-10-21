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
