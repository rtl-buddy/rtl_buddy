## Run two tiers at once

One regression per simulator in one checkout, concurrently, needs a per-run artefact namespace — otherwise both runs write the same `artefacts/<test>/` paths and the second dies on the [artefact-tree lock](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#handle-an-artefact-lock). Give each run a `--run-tag`:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
rb graph results --run-tag verilator
rb graph results --run-tag icarus
```

Each run gets its own tree, its own lock, its own log, and its own results overlay under `artefacts/.runs/<tag>/`; the shared builds stay shared. See [Namespace concurrent runs](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#namespace-concurrent-runs) for the full path table and the tag's syntax rules.
