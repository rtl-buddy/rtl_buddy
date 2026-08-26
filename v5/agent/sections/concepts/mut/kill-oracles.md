## Kill oracles

A mutant is *killed* when a configured oracle flags it. You configure at least one of two oracles in the `verify:` block (you may configure both — a mutant is killed if **either** oracle catches it):

| Oracle | Configured by | A mutant is killed when |
|---|---|---|
| **FPV** | `fpv_config` + `verification` | the named verification's proof flips from the unmutated baseline `PASS` to `FAIL` |
| **Simulation** | `test_config` (+ optional `tests`, `assertions`) | a test in the suite `FAIL`s or an SVA assertion fires |

The simulation oracle compiles SVA in via Verilator `--assert` by default (`assertions: true`); it is much weaker without assertions, so leave them on unless you have a reason not to. See [Assertion-Based Verification (sim)](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/abv-simulation/) for how firings are detected.
