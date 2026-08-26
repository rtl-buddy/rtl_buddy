## Run and score

```bash
rb mut list
rb mut list -c mut/demo/mut.yaml
rb mut run -c mut/demo/mut.yaml
rb mut score mut/demo/artefacts/mut/demo_top/mut_report.json
```

- `list` shows candidate sites without mutation.
- `run` uses `debug` builder mode by default, evaluates the baseline and mutants, then writes the report.
- `score` recomputes the score from an existing report without rerunning verification.

Paths and artefacts are anchored to the selected `mut.yaml`, not the shell working directory. See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/).
