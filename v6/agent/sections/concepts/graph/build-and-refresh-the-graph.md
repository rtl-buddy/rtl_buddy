## Build and refresh the graph

`rb graph build` covers every model under the design directory and writes `artefacts/graph/graph.json`. `rb graph results` then adds current test and coverage state in a separate overlay file; rerun it after tests or coverage runs.

- Narrow a build with a repeatable `--model NAME` or with `-c/--regression FILE`. The two are mutually exclusive.
- `--no-design`, `--no-tb`, `--no-flow-tops`, `--no-bind` and `--no-extract` skip individual parts; `--force` ignores the cache. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/#graph).

A build has three tiers: the design hierarchy from `rtl-buddy-view`, the declarations in rtl_buddy configs, and bindings (cocotb, Python imports, signal access, golden models, DPI). An optional external binding tier is added when `rtl-buddy-graph-extract` is installed; without it that tier is `skipped` and the graph stays usable.

The design tier needs a compatible `rtl-buddy-view`; check it with `rb tool-check --explain rtl-buddy-view`. A missing or incompatible `rtl-buddy-view` makes the design tier `failed`. Per-model failures make the command exit non-zero only with `--strict`. `graph-meta.json` records each tier's status and failures.
