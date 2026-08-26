## Running

```bash
# List candidate mutation sites without mutating (uses ./mut.yaml)
rb mut list

# A specific config
rb mut list -c mut/demo/mut.yaml

# Generate mutants, score them against the oracles, and write a report
rb mut run -c mut/demo/mut.yaml

# Recompute the score from a saved report (no re-run)
rb mut score mut/demo/artefacts/mut/demo_top/mut_report.json
```

`rb mut run` builds each mutant in `debug` builder mode by default. All three subcommands take `-c`/`--mut-config` (default `mut.yaml`) and, like every command, anchor their artefact tree on the directory containing the selected `mut.yaml` — not your shell's cwd (see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/)).
