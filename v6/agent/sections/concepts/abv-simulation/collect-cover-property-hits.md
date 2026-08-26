## Collect cover-property hits

`assertions: true` also enables Verilator user coverage, so labeled `cover property` hits appear in each run's `coverage.dat`. Use the normal coverage flags to merge or report them:

```bash
rb -M cov test smoke_with_sva --coverage-merge
```

Under `--machine`, cover points are reported by name per test and summed across the run. See [Coverage](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/coverage/#inspect-cover-property-hits) for the data and merge behavior.

Simulation checks only the stimulus that ran. Use [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpv/) when the property needs bounded proof over all modeled behaviors.
