## Choose kill oracles

At least one oracle is required. When both are configured, either can kill a mutant.

| Oracle | Required fields | A mutant is killed when |
|---|---|---|
| FPV | `fpv_config`, `verification` | The named baseline PASS becomes FAIL |
| Simulation | `test_config`; optional `tests`, `assertions` | A selected test fails or an SVA assertion fires |

Simulation enables Verilator assertions by default; see [Assertion-based verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/abv-simulation/).
