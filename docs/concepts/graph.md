---
description: Build and query rtl_buddy's design knowledge graph, join test and coverage results, and use its CLI, MCP, and browser interfaces.
---

# Design Knowledge Graph

The design knowledge graph connects project configuration, elaborated RTL hierarchy, tests, specifications, and source bindings into one queryable graph. Query it from the CLI, an MCP client, or the hub.

```bash
rb graph build
rb graph results
rb graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
```

## Choose graph or source lookup

Use the graph for transitive impact through elaborated hierarchy, paths that cross design, test, specification and binding data, and current results or coverage joined to those relationships. For a port list, a YAML field or another single-file fact, use the graph to find the file and read the lines. Do not enumerate a small config file through many `explain` calls.

## Build and refresh the graph

`rb graph build` covers every model under the design directory and writes `artefacts/graph/graph.json`. `rb graph results` then adds current test and coverage state in a separate overlay file; rerun it after tests or coverage runs.

- Narrow a build with a repeatable `--model NAME` or with `-c/--regression FILE`. The two are mutually exclusive.
- `--no-design`, `--no-tb`, `--no-flow-tops`, `--no-bind` and `--no-extract` skip individual parts; `--force` ignores the cache. See the [CLI reference](../reference/cli.md#graph).

A build has three tiers: the design hierarchy from `rtl-buddy-view`, the declarations in rtl_buddy configs, and bindings (cocotb, Python imports, signal access, golden models, DPI). An optional external binding tier is added when `rtl-buddy-graph-extract` is installed.

The design tier needs a compatible `rtl-buddy-view`; check it with `rb tool-check --explain rtl-buddy-view`. A tier whose tool is missing is `skipped` and the graph stays usable; a requested tier that breaks is `failed`. Per-model failures make the command exit non-zero only with `--strict`. `graph-meta.json` records each tier's status and failures.

## Cached builds

A build with the same inputs, tool versions and tier selection does nothing. A cached failure stays cached until an input changes; pass `--force` to retry.

## Models with no elaborable root

The design tier elaborates each model from its `top:`, which defaults to the model name. If no module has that name (an SV `interface` published as a library entry, a filelist of vendored IP), set `top:` to the real root module or set `graph: false` in [`models.yaml`](../reference/yaml.md#modelsyaml).

- A `graph: false` model is listed under the design tier's `skipped` entries, never `failures`, and does not change the exit code even with `--strict`. If every model in scope opts out, the whole tier is `skipped`.
- Its cocotb testbenches and synth, CDC and FPGA runs that would elaborate the same root are skipped with it.
- Opting out a model exported earlier deletes its `artefacts/graph/design/<model>/` directory, so no stale hierarchy is served.
- The config tier keeps a node for the model, so `spec:` and test references resolve, but adds no edge into the design tier. A model outside the `--model` or `-c` selection is treated the same way.
- `rb hier`, `rb hier-query` and `rb axi-profile` ignore `graph: false`.

## Model names and tops must be distinct

Every selected model needs a distinct `name`, and every exported model needs a distinct top module, because `module:<top>` is a global node id. `graph build` refuses a violation before running the exporter and names both models and both `models.yaml` files. Rename one of two models with the same name; `graph: false` does not help. For two models with the same top, give one a different `top:` or set `graph: false` on it.

## Query the graph

Run the read verbs from the project root:

```bash
rb graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb graph path cocotb_random module:demo_tiny_alu
rb graph explain test:verif/demo_tiny_alu#flags
```

- `query` does keyword matching with bounded neighbourhood expansion. Narrow it with `--type`, `--tier`, `--depth` or `--limit`.
- `path` returns shortest paths. Traversal is undirected by default, because edge direction expresses role, not reachability. Pass `--directed` when direction matters.
- `explain` returns one node's attributes, edges, test result, coverage entry, and, for instance nodes, a command that cites the source.

A bare name works only when it identifies one node; otherwise the command fails with the candidate ids. `query` exits 1 when nothing matches, and an invalid or ambiguous node reference exits 2. The verbs take no write lock and can run during a regression. Pass `--no-results` for a structural-only answer.

With `--machine` each verb emits the standard [machine envelope](../agents.md#machine-mode). Truncation metadata reports neighbours cut off by bounded expansion; raise the limit or explain a specific peer rather than assuming the result is complete.

## Results Overlay

`rb graph results` writes `artefacts/graph/results-overlay.json` and never modifies `graph.json`. Add `--strict` to fail on the mismatches listed below.

Each entry is keyed by `test:<suite dir>#<test name>` and holds the latest status (from the test's `result.json`), seed, timestamp, compile duration and existing artefact paths. A test directory with artefacts but no result is `UNKNOWN`. Random-test iterations are listed under `runs`, and the newest gives the top-level status.

The refresh reports three mismatches against `graph.json`:

- `missing`: graph test nodes with no result.
- `unmatched`: results with no declared test node, such as generated sweep names.
- `problems`: unreadable result data.

To convert one regression's results, pass the tag it ran with:

- `--run-tag <name>` reads `<suite>/artefacts/.runs/<tag>/` and writes `artefacts/.runs/<tag>/graph/results-overlay.json`, so concurrent regressions do not collide.
- The hub, the MCP server and `rb graph query` read the untagged overlay. Publish a tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.

## Coverage on the Graph

`rb graph results` also joins each declared `covers:` relationship to coverage already on disk. It never reruns the simulator. See [Coverage](coverage.md) for what the metrics mean.

The default `--coverage auto` uses the newest coverage manifest and model, and falls back to per-test `coverage.dat` files. Other choices:

- `--cov-dir` or `--cov-manifest` selects a manifest.
- A merged LCOV `.info` file can be passed as the source.
- `--coverage model` requires the model source.
- `--no-coverage` disables the join.

Each coverage item gets one state:

| State | Meaning |
| --- | --- |
| `exercised` | A declared item matched an observed cover point with hits. |
| `declared-only` | The item was declared but no matched point fired. |
| `observed-but-undeclared` | An observed cover point has no `covers:` declaration. |

Names match exactly first, then case-insensitively, then after normalisation, then with a `cov`/`cvr`/`c` affix removed. The overlay records which rule matched; an `affix` match is a prompt to align the names. LCOV has no module or per-test identity, so it joins design coverage by source file only. Unresolved paths are reported, not guessed.

## Physical Heat on the Graph

The `/gph` pane can fill module nodes with the physical model that `rb synth` and `rb power` write, the same data as the [`/phy` pane](phys.md#browse-the-model-in-the-hub).

- Tick `heat` in the header. The model loads on the first tick, because a mapped design's power data is megabytes.
- `metric` offers `cells`, `area`, `leakage`, `dynamic` and `total`. `run` selects the artefact directory; `/gph?dir=<phys dir>` opens the pane on one run.
- A run the server refuses (no manifest, or outside the project) leaves the current model and shows the refusal in the status line. A bad `?dir=` on an empty pane falls back to the newest run and says so.
- Each node shows its value in a badge, every metric in its tooltip and the full row in the inspector. Coverage and heat share the node fill, so enabling one turns off the other.

Cells and area come from a module's definition and are counted once however often it is instantiated; area already includes submodules. Power is summed over every instantiation, so eight FIFOs give one FIFO row of area and eight FIFOs' leakage. The inspector prints `instances` and `leaf rows` beside the figures. Do not add the two halves together or divide power by area. Power is attributed by instance path, so an incomplete design tier degrades it; see [Known Issues](../known-issues.md#graph-pane-heat-attributes-a-leaf-to-the-nearest-instance-the-graph-knows).

A `phys-focus` from a script turns heat on, loads the model if needed, and highlights the node:

```bash
rb hub send phys-focus module:dma_engine --metric area
```

## Looking at the Graph

Serve the pane through the hub and open `http://127.0.0.1:<http_port>/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

The pane rereads the graph and overlay on reload. It can tint design nodes with joined coverage, or module nodes with physical heat. Clicking a node focuses the schematic or opens the source in a connected editor. Focus a node from a script with:

```bash
rb hub send graph-focus module:dma_engine
```

See [Hub](hub.md#design-knowledge-graph-pane) for pane behavior.

## The MCP Server

Install the optional dependency, list the tools, and configure the agent host to launch the stdio server:

```bash
uv add 'rtl_buddy[mcp]'
rb mcp --list-tools
```

```json
{
  "mcpServers": {
    "rtl-buddy": {"command": "rb", "args": ["mcp"]}
  }
}
```

Graph, test-status, coverage, physical-metrics and hierarchy tools mirror their `rb --machine` payloads. Each call rereads the artefact files, so updates show up without restarting the server. When a live hub is discoverable, the server also offers tools for hub state, selection, source opening, diagnostics, and coverage and physical focus; without a hub they are omitted.

## Graph data model

`graph.json` is a directed [NetworkX node-link](https://networkx.org/documentation/stable/reference/readwrite/json_graph.html) multigraph. Every node has `id`, `type`, `label` and `tier`; every edge has `type` and `confidence`. Paths in ids are project-relative. When different testbench files declare the same module name, testbench ids get an `@<suite dir>` qualifier; use the full id when a label is ambiguous.

| Tier | Contents |
| --- | --- |
| `design` | Modules, instances, ports, parameters, interfaces, and modports from `rtl-buddy-view`. |
| `config` | Suites, tests, testbenches, flow runs, models, specs, coverage items, docs, and golden models. |
| `binding` | Python modules, imports, cocotb-to-DUT and signal bindings, golden-model checks, and DPI implementations. |

Node ids, which you pass to `path` and `explain`:

- Design: `module:<name>`, `inst:<top>/<dot.path>`, `port:<module>.<port>`, `param:<module>.<name>`, `iface:<name>`, `modport:<interface>.<name>`.
- Config: `suite:<suite dir>`, `test:<suite dir>#<name>`, `tb:<suite dir>#<name>`, `model:<models.yaml>#<name>`, `spec:<block>`, `covitem:<block>#<id>`, `doc:<path>`, `golden:<path>`.
- Binding: `py:<path>`.

Edge types:

- Design: `instantiates`, `child_of`, `instance_of`, `connects`, `implements`, `overrides`.
- Config: `declares` (suite to test or testbench, spec block to coverage item), `runs_on` (test to testbench), `exercises` (testbench or run to model), `covers` (test or formal run to coverage item), `specified_by` and `documented_by` (traceability), `maps_to` (model to module), `elaborates_as` and `targets` (testbench or run to its top module).
- Binding: `binds_to`, `imports`, `drives`, `checks_against`, `implemented_by`. `drives` and `implemented_by` can be `INFERRED`; an unresolved match has `resolved: false`.

Results, seeds and timestamps live only in the overlay. `graph.json` is plain JSON; NetworkX is optional:

```python
import json
import networkx as nx

with open("artefacts/graph/graph.json") as handle:
    graph = nx.node_link_graph(json.load(handle), edges="links")
```

## Troubleshooting

Build errors:

- `N models are named 'X'`: rename one; the message lists the `models.yaml` files.
- `two or more models would be exported with the same top module`: give one a distinct `top:` or set `graph: false` on it.
- `declares graph: false, but its previous design-tier export could not be removed`: fix the permissions on the named directory or delete it, then rerun.
- `refusing to retract the export of model 'X'`: `artefacts/graph/design/<model>` resolves outside the design directory. Fix the model name or remove the symlink.

Build warnings (the build continues):

- `graph_build.design_export_failed`, `tb_export_failed`, `run_export_failed`: `rtl-buddy-view graph` failed. The event names its log; fix the elaboration error and rebuild.
- `graph_build.extract_failed`: the external binding tier failed; the graph is built without it.
- `graph_build.extract_merge_mismatch`: external and built-in bindings disagree. `graph-meta.json` lists examples.
- `graph_build.tb_id_collision`: testbench ids from different suites collided and were qualified with `@<suite dir>`.
- `graph_merge.node_type_conflict`: two tiers gave one node id different types.
- `graph_config.regression_load_failed`, `suite_load_failed`: a regression or `tests.yaml` file did not load, so its tests are missing. Fix the file.
- `graph_bind.cocotb_module_not_found`, `dpi_symbol_not_found`: the source for a test's cocotb module or DPI symbol was not found; the binding stays unresolved.

Query and overlay:

- `no graph at <path>; run rb graph build first`: build the graph.
- `is not valid JSON` or `is not node-link JSON`: run `rb graph build --force`.
- `'X' matches N nodes; use a full node id`, `no node matches 'X'`: pick from the listed candidate ids.
- `graph_results.overlay_rejected`: the overlay has the wrong type or schema version and is ignored. Rerun `rb graph results`.
- `graph_coverage.unavailable`: the coverage source was missing or unreadable. The join is skipped and the reason is listed under `problems`.
