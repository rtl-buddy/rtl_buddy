## What you see in the results table

When at least one test in the run enables `assertions`, both `rb test` and `rb regression` add an **Assertions** column:

```text
Test           Result   Description                    Assertions
smoke_with_sva PASS     test passed                    0 fired
sva_violation  FAIL     1 SVA assertion failure(s) …   1 fired
```

- `0 fired` confirms SVA was compiled in and no `%Error: ... Assertion failed` lines were seen.
- `N fired` reports the count; the test is forced to FAIL even if the testbench wrapper printed PASS earlier.

The column is hidden when no test in the run requests assertions, so existing flows are unchanged.
