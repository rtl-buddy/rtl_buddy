---
description: Render or query a model or testbench hierarchy with `rb hier`, `rb hier-query`, and the external `rtl-buddy-view` renderer.
---

# Hierarchy Rendering

Use `rb hier` to see a design's hierarchy as text or diagram source. Use `rb hier-query` when a script or agent needs one structural answer.

## Install the renderer

`rb hier` runs the `rtl-buddy-view` executable, which the `rtl-buddy-sch` distribution provides:

```bash
uv tool install rtl-buddy-sch
rb tool-check --explain rtl-buddy-view
```

Use `--tool /absolute/path/to/rtl-buddy-view` to pin a development build. Optional dependencies:

- Graphviz `dot`, to convert DOT to SVG or PNG.
- `pyslang`, for `--frontend slang`.

See [Installation](../install.md#external-tools-by-feature) for tool setup. If an older `rtl-buddy-view` package conflicts with `rtl-buddy-sch`, see [Known Issues](../known-issues.md#the-viewer-distribution-and-executable-have-different-names).

## Render a hierarchy

Run from a directory where rtl_buddy can find the configuration, or pass `-c`:

```bash
rb hier demo_top
rb hier demo_top --format mermaid -o demo_top.mmd
rb hier demo_top --format dot | dot -Tsvg -o demo_top.svg
rb hier demo_top --format json -o demo_top.hier.json
rb hier demo_top -c design/demo_top/models.yaml
```

The positional name selects a model from `models.yaml`. rtl_buddy builds a stripped, deduplicated filelist from it and passes the model name to the renderer as the top.

| Format | Use |
| --- | --- |
| `tree` | Terminal inspection (default). |
| `dot` | Graphviz input for diagrams. |
| `mermaid` | Mermaid source for Markdown. |
| `json` | Structured data for downstream tools. |

Without `-o` the output goes to stdout and can be piped. With `-o` the renderer writes the file.

## Render a testbench hierarchy

`--view tb` shows the hierarchy above and around the DUT:

```bash
rb hier basic_traffic --view tb
```

Here the positional name is a test from `tests.yaml`, not a model. The test supplies the DUT model and testbench top. Tests with the same `(model, testbench)` reuse one generated hierarchy artefact.

## Render a block diagram

For sibling dataflow instead of an instantiation tree, generate block-diagram DOT:

```bash
rb hier demo_top --format dot --block-diagram | dot -Tsvg -o demo_top_block.svg
```

This needs `rtl-buddy-sch >= 0.8.0`; older renderers fail with an upgrade message. It applies only to DOT output.

## Query hierarchy data

`rb hier-query` answers a focused question without rendering the whole hierarchy:

```bash
rb hier-query demo_top find-module axi_arbiter
rb hier-query demo_top subtree demo_top.u_fabric --format tree
rb hier-query demo_top instances-of axi_arbiter
rb hier-query demo_top port-connections demo_top.u_fabric.u_arb0
rb hier-query demo_top source-snippet demo_top.u_fabric.u_arb0 --context 4
```

- `find-module` and `instances-of` take a module name. The other verbs take a dot-separated instance path starting at the model's root module (`top:` in `models.yaml`, default the model name).
- Results are JSON, except `source-snippet`, which prints source text with line numbers by default.
- A lookup miss or parse failure exits 1 and prints the viewer diagnostic to stderr. An empty `instances-of` result is a valid answer and exits 0.

## Select a parser and annotations

Pass `--frontend slang` for SystemVerilog the default parser cannot elaborate. The renderer validates frontend names.

Domain overlays are JSON maps keyed by hierarchical instance path:

```bash
rb hier demo_top --format dot --clock-legend | dot -Tsvg -o clocks.svg
rb hier demo_top --format dot --rdc-annotations resets.json | dot -Tsvg -o resets.svg
```

rtl_buddy checks that annotation files exist before starting the renderer. `--clock-legend` applies only to DOT output.

## Find outputs and diagnose failures

Outputs are anchored to the primary config's directory, not the shell's current directory. A model render writes:

```text
<models.yaml directory>/artefacts/hier/<model>/
├── hier.f
└── hier.log
```

A testbench render writes to `artefacts/hier/<model>/tb/<testbench>/` under the `tests.yaml` directory. `hier.f` is the generated filelist and `hier.log` holds the renderer's stderr. Queries also write `query.log`.

`rb hier` exits with the renderer's exit code. For a parse, elaboration, or output failure, read `hier.log`. If the executable is not found, run `rb tool-check --explain rtl-buddy-view`.

For interactive browsing, `rb hub start --serve-viewer --model <name>` serves the same JSON hierarchy. See [Hub](hub.md).
