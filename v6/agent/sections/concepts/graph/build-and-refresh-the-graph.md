## Build and refresh the graph

Build structural data first, then refresh the independent results overlay after tests or coverage runs:

```bash
rb graph build
rb graph results
```

By default, `graph build` finds every model under the design directory. Select a smaller scope with a repeatable `--model NAME` or with `-c/--regression FILE`; those two selectors are mutually exclusive.

The normal build includes:

- DUT, testbench, and non-simulation run hierarchies from `rtl-buddy-view`.
- Test, model, regression, specification, and coverage declarations from rtl_buddy configs.
- cocotb, Python import, signal-access, golden-model, and DPI bindings.
- An optional external binding tier when `rtl-buddy-graph-extract` is installed.

Useful reductions are `--no-design`, `--no-tb`, `--no-flow-tops`, `--no-bind`, and `--no-extract`. Use `--force` to ignore a valid cache. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/#graph) for the complete option list.

The design tier requires a compatible `rtl-buddy-view`:

```bash
rb tool-check --explain rtl-buddy-view
```

The external extractor is optional. If absent, its tier is `skipped` and the graph remains usable. A requested tier that breaks is `failed`; per-model failures become non-zero only with `--strict`. Inspect `graph-meta.json` for tier status and failure details.

Unchanged inputs, tool versions, and schema produce a cached no-op build. A failure remains cached until an input or tool version changes, or `--force` is used.
