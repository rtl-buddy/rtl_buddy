## Run two tiers at once

Two regressions in one checkout write the same `artefacts/<test>/` paths, and the second fails on the [artefact-tree lock](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#handle-an-artefact-lock). Give each a `--run-tag`:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
```

Each run gets its own tree, lock, log and results overlay under `artefacts/.runs/<tag>/`, while shared builds stay shared. Convert each run's results with `rb graph results --run-tag <tag>`. See [Namespace concurrent runs](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#namespace-concurrent-runs) for the path table and tag rules.
