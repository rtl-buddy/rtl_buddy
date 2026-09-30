## Run and score

```bash
rb mut list
rb mut list -c mut/demo/mut.yaml
rb mut run -c mut/demo/mut.yaml
rb mut score mut/demo/artefacts/mut/demo_top/mut_report.json
```

- `list` shows candidate sites without mutating.
- `run` uses the `debug` builder mode by default, evaluates the baseline and the mutants, then writes the report.
- `score` recomputes the score from an existing report without rerunning verification.

Paths and artefacts anchor to the selected `mut.yaml`, not the shell directory; see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/).
