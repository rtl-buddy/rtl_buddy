---
description: Render or query a model or testbench hierarchy with `rb hier`, `rb hier-query`, and the external `rtl-buddy-view` renderer.
---

# Hierarchy Rendering

Use `rb hier` to inspect hierarchy as text or diagram source. Use `rb hier-query` when a script or agent needs a specific structural answer.

## Install the renderer

`rb hier` shells out to the `rtl-buddy-view` executable. Install the current `rtl-buddy-sch` distribution:

```bash
uv tool install rtl-buddy-sch
rb tool-check --explain rtl-buddy-view
```

Use `--tool /absolute/path/to/rtl-buddy-view` to pin a development build. Optional dependencies are:

- Graphviz `dot` to convert DOT into SVG or PNG.
- `pyslang` when using `--frontend slang`.

See [Installation](../install.md#external-tools-by-feature) for tool setup and [Known Issues](../known-issues.md#the-viewer-distribution-and-executable-have-different-names) if an older `rtl-buddy-view` package conflicts with `rtl-buddy-sch`.

## Render a hierarchy

Run from a directory where rtl_buddy can find the relevant configuration, or pass `-c` explicitly:

```bash
rb hier demo_top
rb hier demo_top --format mermaid -o demo_top.mmd
rb hier demo_top --format dot | dot -Tsvg -o demo_top.svg
rb hier demo_top --format json -o demo_top.hier.json
rb hier demo_top -c design/demo_top/models.yaml
```

The positional name selects a model from `models.yaml`. rtl_buddy builds a stripped, deduplicated filelist from that model and passes its name as the renderer top.

Available formats are:

| Format | Use |
| --- | --- |
| `tree` | Terminal inspection; default. |
| `dot` | Graphviz input for diagrams. |
| `mermaid` | Mermaid source for Markdown. |
| `json` | Structured data for downstream tools. |

Without `-o`, renderer output goes to stdout and can be piped. With `-o`, the renderer writes the requested file.

## Render a testbench hierarchy

Use TB view when you need the hierarchy above and around the DUT:

```bash
rb hier basic_traffic --view tb
```

In this mode the positional name selects a test from `tests.yaml`, not a model. The test identifies the DUT model and testbench top. Tests that share the same `(model, testbench)` reuse the generated hierarchy artefact.

## Render a block diagram

For sibling dataflow instead of an instantiation tree, generate block-diagram DOT:

```bash
rb hier demo_top --format dot --block-diagram | dot -Tsvg -o demo_top_block.svg
```

This requires `rtl-buddy-sch >= 0.8.0`. Older renderers fail with an upgrade instruction. The option is meaningful only with DOT output.

## Query hierarchy data

`rb hier-query` returns focused answers without rendering the full hierarchy:

```bash
rb hier-query demo_top find-module axi_arbiter
rb hier-query demo_top subtree demo_top.u_fabric --format tree
rb hier-query demo_top instances-of axi_arbiter
rb hier-query demo_top port-connections demo_top.u_fabric.u_arb0
rb hier-query demo_top source-snippet demo_top.u_fabric.u_arb0 --context 4
```

`find-module` and `instances-of` take a module name. The other verbs take a dot-separated instance path rooted at the model's root module (its `top:` in `models.yaml`, defaulting to the model name). Results are JSON except `source-snippet`, which prints source text with line numbers by default.

A lookup miss or parse failure exits 1 and prints the viewer diagnostic to stderr. An empty `instances-of` result is a successful answer and exits 0.

## Select a parser and annotations

Pass `--frontend slang` for SystemVerilog that the default parser cannot elaborate. Frontend names are validated by the renderer.

Domain overlays are JSON maps keyed by hierarchical instance path:

```bash
rb hier demo_top --format dot --clock-legend | dot -Tsvg -o clocks.svg
rb hier demo_top --format dot --rdc-annotations resets.json | dot -Tsvg -o resets.svg
```

rtl_buddy checks that annotation files exist before starting the renderer. `--clock-legend` applies only to DOT output.

## Find outputs and diagnose failures

Outputs are anchored to the primary configuration directory, not the shell's current directory. A model render writes:

```text
<models.yaml directory>/artefacts/hier/<model>/
├── hier.f
└── hier.log
```

TB view writes under the `tests.yaml` directory at `artefacts/hier/<model>/tb/<testbench>/`. `hier.f` is the generated filelist and `hier.log` captures renderer stderr. Query invocations also write `query.log`.

`rb hier` returns the renderer's exit code. For parse, elaboration, or output failures, inspect `hier.log`. If the executable cannot be found, run:

```bash
rb tool-check --explain rtl-buddy-view
```

For interactive browsing, `rb hub start --serve-viewer --model <name>` builds and serves the same JSON hierarchy. See [Hub](hub.md).
