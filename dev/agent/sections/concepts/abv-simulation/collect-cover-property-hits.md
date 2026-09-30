## Collect cover-property hits

`assertions: true` also enables Verilator user coverage, so labeled `cover property` hits appear in each run's `coverage.dat`. Merge or report them with the normal coverage flags:

```bash
rb -M cov test smoke_with_sva --coverage-merge
```

Under `--machine`, cover points are reported by name per test and summed over the run. See [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/#inspect-cover-property-hits).

Simulation checks only the stimulus that ran. When a property needs a bounded proof over all modeled behavior, use [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpv/).
