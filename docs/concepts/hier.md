---
description: How to render module hierarchy diagrams with rtl_buddy via the rb hier command and the standalone rtl-buddy-view renderer.
---

# Hierarchy Rendering

> **Integration type:** Pluggable — curated. `rb hier` shells out to the standalone [rtl-buddy-view](https://github.com/rtl-buddy/rtl-buddy-view) renderer at subprocess granularity; rtl_buddy is not coupled to its Python API.
>
> **External binary required:** `rtl-buddy-view` — install with `uv tool install rtl-buddy-view` (or `pip install rtl-buddy-view`).
>
> **Optional:** `graphviz` (`dot`) for SVG/PNG conversion of `--format dot`; `pyslang` for `--frontend slang`.
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb hier <model>` produces a module-hierarchy view of a model defined in `models.yaml`. It writes a stripped, deduplicated filelist into `artefacts/hier/<model>/hier.f`, then runs `rtl-buddy-view --top <model> --filelist hier.f` with the requested format and forwards the renderer's stdout to the terminal so it composes with shell pipes.

`rb hier <test> --view tb` (rtl-buddy-view #99 / 6b) renders the **testbench** hierarchy for a test from `tests.yaml`, with the DUT called out as a subtree. The test pins both the model (DUT side) and the TB top, so the positional argument is a test name in this mode. The merged DUT + TB filelist is written to `artefacts/hier/<model>/tb/<tb_name>/hier.f` and the renderer is invoked with both `--top <model>` and `--tb-top <tb.toplevel>`. Cache key is `(model, tb_name)`, so two tests sharing a TB share the artefact.

## Installing rtl-buddy-view

```bash
uv tool install rtl-buddy-view    # recommended — isolated tool env
# or
uv pip install rtl-buddy-view     # into the project venv
```

Once installed, the binary lands on `PATH` as `rtl-buddy-view`. Override with `--tool /absolute/path/to/rtl-buddy-view` if you need a specific build.

For `--format dot` rendering to SVG/PNG, install Graphviz (`brew install graphviz` / `apt install graphviz`) and pipe the output through `dot` (see [Output formats](#output-formats) below).

## Output formats

`--format` selects one of four renderers:

| Format | What you get |
|--------|--------------|
| `tree` (default) | ASCII tree, ideal for terminal inspection |
| `dot` | Graphviz DOT source — pipe through `dot -Tsvg` / `-Tpng` for graphics |
| `mermaid` | Mermaid diagram source — paste into Markdown that renders Mermaid |
| `json` | Structured JSON (schema_version, tool.\*, design.top, nodes, edges) for programmatic consumption |

When `-o`/`--output` is not set, the renderer's stdout passes through to your terminal so `rb hier x --format dot | dot -Tsvg -o x.svg` works as a one-liner.

## Running `rb hier`

```bash
# ASCII tree (default)
rb hier demo_top

# Save Mermaid source to a file
rb hier demo_top --format mermaid -o demo_top.mmd

# DOT → SVG via Graphviz
rb hier demo_top --format dot | dot -Tsvg -o demo_top.svg

# JSON export for downstream tooling
rb hier demo_top --format json -o demo_top.hier.json

# Point at a non-default models.yaml
rb hier demo_top -c design/demo_top/models.yaml

# Pin a renderer build
rb hier demo_top --tool /opt/rtl-buddy-view/bin/rtl-buddy-view

# TB-rooted view — render the testbench for a test, DUT called out as subtree
rb hier basic_traffic --view tb
```

The model argument matches the `name:` of an entry in `models.yaml`. The runner uses that entry's filelist verbatim — same source of truth that `rb test`, `rb synth`, and `rb cdc` consume. In `--view tb` mode the positional argument is a test name from `tests.yaml` instead; the test pins both the model (DUT side) and the testbench top, and the renderer merges the model + TB filelists before elaborating from `--tb-top`.

## Querying with `rb hier-query`

`rb hier-query <model> <verb> <arg>` is `rb hier`'s machine-readable sibling: instead of rendering a diagram, it answers a structural question about the model's hierarchy with JSON on stdout, ready for `jq`, shell pipelines, and agent tool use. It shells out to `rtl-buddy-view query` (requires rtl-buddy-view ≥ 0.3.0) and shares `rb hier`'s generated filelist artefact.

```bash
# the full definition of a module: ports, parameters, instances
rb hier-query demo_top find-module axi_arbiter

# the hierarchy subtree below an instance path (add --format tree for ASCII)
rb hier-query demo_top subtree demo_top.u_fabric

# every instance of a module, as instance paths
rb hier-query demo_top instances-of axi_arbiter | jq -r '.[].instance_path'

# the .port(net) connection list of one instance
rb hier-query demo_top port-connections demo_top.u_fabric.u_arb0

# the module-definition source of an instance, line-number-prefixed for citation
rb hier-query demo_top source-snippet demo_top.u_fabric.u_arb0 --context 4
```

Verb arguments are a module name (`find-module`, `instances-of`) or a dot-separated instance path rooted at the model name (`subtree`, `port-connections`, `source-snippet`). `source-snippet` prints plain text rather than JSON — its output is the line-number-prefixed citation block itself (`--no-line-numbers` disables the prefixes, `--context N` widens the window).

Exit codes follow the query semantics: `0` for an answer (an empty `instances-of` list is a valid answer), `1` for a lookup miss or parse failure — the viewer's diagnostic (e.g. `query: instance path '…' not found`) streams to stderr rather than being captured into a log. `artefacts/hier/<model>/query.log` records the underlying invocation.

## Parser frontend

`--frontend` is forwarded as-is to `rtl-buddy-view`. The default frontend ships with the renderer; `--frontend slang` uses pyslang for SystemVerilog constructs the default frontend doesn't parse. rtl_buddy does not validate the set of accepted frontends — that lets the renderer add frontends without an rtl_buddy release. Unknown values are rejected by the renderer's own argument parser.

## CDC and RDC annotations

`rb hier` can overlay clock-domain and reset-domain information when the corresponding analyzer pass has emitted a JSON map:

```bash
# Clock-domain overlay — colors each module by its primary clock
rtl-buddy-cdc --emit-domain-map -o clocks.json ...
rb hier demo_top --format dot --cdc-annotations clocks.json | dot -Tsvg -o hier.svg

# With a side legend mapping color → clock name (dot format only)
rb hier demo_top --format dot --cdc-annotations clocks.json --clock-legend | dot -Tsvg -o hier.svg

# Reset-domain overlay — colors each module by its primary reset
rtl-buddy-cdc --emit-reset-domain-map -o resets.json ...
rb hier demo_top --format dot --rdc-annotations resets.json | dot -Tsvg -o hier.svg
```

Both annotation files are JSON keyed by hierarchical instance path. `rb hier` validates that the files exist before invoking the renderer; the renderer's JSON contract (`schema_version`, `tool.*`, `design.top`, `nodes`, `edges`) is the integration boundary.

`--clock-legend` is honored only for `--format dot`; the tree and Mermaid renderers ignore it.

## Artefacts

Per-model outputs land under the model's command root — `<dir of models.yaml>/artefacts/hier/<model>/` (in `--view tb` mode, `<dir of tests.yaml>/artefacts/hier/<model>/tb/<tb_name>/`). The artefact tree is anchored on the primary config's directory, not your shell's cwd — see [Execution Context](execution-context.md). For example, `rb hier demo_top -c design/demo_top/models.yaml` writes under `design/demo_top/artefacts/hier/demo_top/`:

| File | Contents |
|---|---|
| `hier.f` | Stripped, deduplicated filelist passed to the renderer |
| `hier.log` | Renderer stderr (its stdout goes to your terminal when `-o` is not set) |

When `-o <path>` is supplied the renderer writes directly to that path; otherwise its stdout is the diagram source itself.

## Pass/fail detection

`rb hier` exits with the renderer's exit code. A non-zero exit means the renderer reported a parse, elaboration, or output error — check `hier.log` for the captured stderr.

The `Failed to locate rtl-buddy-view` error before the renderer runs is the most common failure mode and indicates that `rtl-buddy-view` is not installed in the active venv or on `PATH`. Run `rb tool-check --explain rtl-buddy-view` for the install hint.

## Hub integration

The [coordination hub](hub.md) consumes `rb hier`'s JSON output (`--format json`) to drive the rtl-buddy-view SPA's interactive hierarchy view. The `rb hier` clock/reset overlays (`--cdc-annotations`, `--rdc-annotations`) are real CLI flags and are surfaced as overlays in the SPA. The AXI-perf overlay is **not** a `rb hier` flag — it is baked into the SPA view only via `rb hub start --axi-perf-from <axi-perf.json>` (see [Hub](hub.md#axi-perf-overlay-and-notebook-spawning)), which invokes the renderer with `--overlay axi-perf=<path>` internally.

`rb hub start --model <name>` discovers the model's `models.yaml`, invokes `rb hier` under the hood, and serves the result alongside live diagnostics and AXI-perf overlays — `rb hier` is the underlying renderer for both the static CLI use case and the live SPA flow.

## Out of scope (today)

- **rtl-buddy-view only.** No alternative hierarchy renderers are wired up. The integration is intentionally subprocess-granularity so a viewer release can be picked up via `uv sync` without code changes here.
- **In-place SVG/PNG.** `rb hier` does not directly emit SVG or PNG — it emits DOT and lets you pipe through Graphviz. This keeps the rtl_buddy ↔ renderer boundary at "text in, text out" and avoids a Graphviz dependency for the common terminal-inspection flow.
