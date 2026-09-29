---
description: Build and query rtl_buddy's design knowledge graph, join test and coverage results, and use its CLI, MCP, and browser interfaces.
---

# Design Knowledge Graph

The design knowledge graph connects project configuration, elaborated RTL hierarchy, tests, specifications, and source bindings. Use it for questions that cross files or need elaborated relationships. Read the file directly for a fact that lives in one file.

## Build and refresh the graph

Build the structural data first, then refresh the results overlay after tests or coverage runs:

```bash
rb graph build
rb graph results
```

`graph build` covers every model under the design directory. Narrow it with a repeatable `--model NAME` or with `-c/--regression FILE`; the two selectors are mutually exclusive. `--no-design`, `--no-tb`, `--no-flow-tops`, `--no-bind` and `--no-extract` skip individual parts, and `--force` ignores a valid cache. See the [CLI reference](../reference/cli.md#graph) for all options.

A build contains:

- DUT, testbench, and non-simulation run hierarchies from `rtl-buddy-view`.
- Test, model, regression, specification, and coverage declarations from rtl_buddy configs.
- cocotb, Python import, signal-access, golden-model, and DPI bindings.
- An optional external binding tier, present when `rtl-buddy-graph-extract` is installed.

The design tier needs a compatible `rtl-buddy-view`; check it with `rb tool-check --explain rtl-buddy-view`. A missing extractor makes its tier `skipped` and the graph stays usable. A requested tier that breaks is `failed`. Per-model failures give a non-zero exit only with `--strict`. `graph-meta.json` records each tier's status and failure details.

## Cached builds

A build with the same inputs, tool versions, schema and tier selection is a cached no-op. The selection includes the flags that narrowed each tier and, for every selected model, its `models.yaml` path, `name`, `top:` and `graph:`.

A cached failure stays cached until an input, tool version or selection changes. Pass `--force` to rebuild anyway.

## Models with no elaborable root

The design tier roots each model's export at the model's `top:`, which defaults to the model name. If no module has that name (an SV `interface` published as a library entry, a filelist of vendored IP), either set `top:` to the real root module or set `graph: false` in [`models.yaml`](../reference/yaml.md#modelsyaml).

A model with `graph: false` is listed under the design tier's `skipped` entries, in the envelope and in `graph-meta.json`, and never under `failures`. `skipped` does not change the exit code, even with `--strict`. If every model in scope opts out, the whole tier is `skipped`. The same listing covers every testbench and non-simulation run rooted at the model:

- A cocotb testbench whose `toplevel:` is the model's root, and synth, CDC or FPGA runs whose top defaults to it, are dropped as redundant when the model is graphable. They are reported when there is no DUT export.
- Testbenches and runs that share one export (same model, filelist and top) are each listed individually.

Opting out a model that was exported before deletes its `artefacts/graph/design/<model>/` directory, including the nested testbench and run exports, so no stale hierarchy is served. If the deletion fails (for example under a read-only artefact directory) the build fails. A symlinked model directory is refused rather than followed. Name collisions are reported before any deletion.

The config tier still emits the model node, marked `graph: false`, so `spec:` and test references resolve. It emits no `maps_to` edge for the model and no `elaborates_as` or `targets` edge from testbenches or runs over it. A testbench keeps its edge when the models its tests name are exported, even if two models share a root module.

- A model outside the build's selection (`--model` or `-c`) gets a node and no edge into the design tier, even though the config tier reads the whole design directory. This keeps `module:<top>` from resolving to another model's hierarchy.
- A `--no-design` build has no selection and stitches every graphable model.
- `rb hier`, `rb hier-query` and `rb axi-profile` ignore `graph: false`: they still elaborate the model and fail if its root does not resolve.

## Model names and tops must be distinct

Every selected model needs a distinct `name`, and every exported model needs a distinct root module. `graph build` refuses either violation before invoking the exporter and names both models and both `models.yaml` files.

- Two models with the same name write to the same `artefacts/graph/design/<name>/` directory and collide in every lookup by name. `graph: false` does not help; rename one.
- Two models with the same top would merge into one hybrid hierarchy, because `module:<top>` is a global id. Set `graph: false` on one of them, since an opted-out model is not exported.

## Query the graph

Use the three read verbs from the project root:

```bash
rb graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb graph path cocotb_random module:demo_tiny_alu
rb graph explain test:verif/demo_tiny_alu#flags
```

- `query` does deterministic keyword matching with bounded neighbourhood expansion. Narrow it with `--type`, `--tier`, `--depth` or `--limit`.
- `path` returns shortest paths. Traversal is undirected by default, since edge direction expresses role, not reachability. Pass `--directed` when direction matters.
- `explain` returns one node's attributes, incident edges, test result, coverage entry, and, for instance nodes, a command that cites the source.

A bare name is accepted only when it identifies one node. An ambiguous name fails with the candidate ids. `query` exits 1 when nothing matches; an invalid or ambiguous node reference exits 2.

The verbs do not take the graph write lock, so they can run during a regression. Pass `--no-results` for a structural-only query.

With `--machine` each verb emits the standard [machine envelope](../agents.md#machine-mode) with the graph and overlay paths and the `matches`, `paths` or explained node. Truncation metadata reports neighbours cut off by bounded expansion. Raise the limit or explain a specific peer rather than assuming the result is complete.

## Choose graph or source lookup

Use the graph for:

- transitive impact through elaborated hierarchy;
- paths that cross design, test, specification and binding data;
- stable node ids and exact structural relationships;
- current results or coverage joined to those relationships.

For a port list, a YAML field or another single-file fact, use the graph to find the file and read the lines. Do not enumerate a small config file through many `explain` calls. Use `explain --expand` only when you need the full attributes of every peer.

After changing query payloads, compare graph and raw-file routes on a built project:

```bash
uv run python scripts/graph_token_benchmark.py -p /path/to/project --markdown
```

## Results Overlay

`rb graph results` writes current run state to `artefacts/graph/results-overlay.json` and never modifies the structural `graph.json`:

```bash
rb graph results
rb graph results --strict
```

Entries are keyed by `test:<suite dir>#<test name>`. Each holds the latest result status, seed, timestamp, and paths to artefacts that exist. The timestamp is the result envelope's modification time, so refreshing unchanged inputs gives identical bytes. Status comes from `result.json`, not from logs. A test directory with artefacts but no envelope is `UNKNOWN`. Random-test iterations are listed under `runs`, and the newest one supplies the top-level status.

An entry has an optional `compile` block (`duration_sec`, `builder`, `reused`) copied from the envelope; it is absent when the envelope has none. For a dispatched run the block is the shared build job's record, not the simulation job's own `reused: true` record. This also holds when a simulation job had to rebuild (`compile.prebuilt_stamp_invalid`).

To convert one run's results, pass the tag used by its `rb regression`:

- `--run-tag <name>` reads `<suite>/artefacts/.runs/<tag>/` and writes `artefacts/.runs/<tag>/graph/results-overlay.json`, so concurrent regressions do not collide. `graph.json` is still read from `artefacts/graph/`.
- The hub, the MCP server and `rb graph query` read the untagged overlay. Publish a tagged run there with `rb graph results --run-tag <name> -o artefacts/graph`.

The refresh cross-checks against `graph.json`:

- `missing` lists graph test nodes with no result.
- `unmatched` lists results with no declared test node, such as generated sweep names.
- `problems` lists unreadable result data.

These are reported normally and fail the command with `--strict`. A missing or unreadable overlay does not stop structural queries.

## Coverage on the Graph

`rb graph results` can join declared `covers:` relationships to coverage already on disk. It never reruns the simulator or rewrites `graph.json`.

The default `--coverage auto` uses the newest coverage manifest and model, then falls back to per-test `coverage.dat` files. Other choices:

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

Names match in this order: exact, case-insensitive, normalized, then `cov`/`cvr`/`c`-affix. The rung used is recorded; an `affix` match is a prompt to align the names. Module coverage matches exact elaborated names first, then names with one trailing parameterization suffix removed, and aggregates multiple elaborations onto the source-module node.

LCOV has no module or per-test identity, so it joins design coverage by resolved file. Per-test databases, when available, still supply test badges and item verdicts. Unresolved, re-anchored or unmatched paths are reported, not guessed. See [Coverage](coverage.md) for metric semantics.

## Physical Heat on the Graph

The `/gph` pane can fill module nodes with the physical model that `rb synth` and `rb power` write. It reads the hub's `GET /phy.json`, the same data as the [`/phy` pane](phys.md#browse-the-model-in-the-hub). Tick `heat` in the header; the model loads on the first tick because a mapped design's power data is megabytes.

- `metric` offers `cells`, `area`, `leakage`, `dynamic` and `total`, as `/phy` does.
- `run` selects the artefact directory, and `/gph?dir=<phys dir>` opens the pane on one run. A run the server refuses (no manifest, outside the project) leaves the current model and shows the refusal in the status line. A bad `?dir=` on a pane with nothing loaded falls back to the newest run and says so.
- The ramp is scaled against the largest module in the graph. Each node shows the value in a badge, every metric in its tooltip and the full row in the inspector.

The synthesis half joins by module name and gives each node its cells and area. The power half joins by instance path: each leaf row's rootless path is resolved to the enclosing RTL instance, and its power is added to that instance's module.

The figures count different things:

- A module's cells and area come from its definition, counted once however often it is instantiated. Area already includes submodules.
- Its power is summed over every instantiation. Eight FIFOs give one FIFO row of area and eight FIFOs' leakage.

The inspector prints `instances` and `leaf rows` beside the figures. Do not add the two halves together or divide power by area. An incomplete design tier degrades the roll-up; see [Known Issues](../known-issues.md#graph-pane-heat-attributes-a-leaf-to-the-nearest-instance-the-graph-knows).

Coverage and heat share the node fill, so enabling one releases the other. An inbound `phys_focus` turns heat on, fetches the model if needed, and highlights the node the target belongs to (a module by name, an instance path by the module holding the leaf), including every suite-qualified export of that module. Clicking a node emits only the pane's own `selection_changed` and `open_source`.

```bash
rb hub send phys-focus module:dma_engine --metric area
```

## Graph data model

`graph.json` is a directed [NetworkX node-link](https://networkx.org/documentation/stable/reference/readwrite/json_graph.html) multigraph. Every node has `id`, `type`, `label` and `tier`; source-backed nodes also have a project-relative `file`. Every edge has `type` and `confidence`. Tiers share one id namespace and merge by node id:

| Tier | Contents |
| --- | --- |
| `design` | Modules, instances, ports, parameters, interfaces, and modports from `rtl-buddy-view`. |
| `config` | Suites, tests, testbenches, flow runs, models, specs, coverage items, docs, and golden models. |
| `binding` | Python modules, imports, cocotb-to-DUT and signal bindings, golden-model checks, and DPI implementations. |

Paths in ids are project-relative and POSIX-separated. When different testbench files declare the same module name, testbench-side ids get an `@<suite dir>` qualifier; DUT ids stay unqualified so the tiers can stitch through them. Use the full qualified id when a label is ambiguous. `reglvl` is stored as written, since builder-specific values need runtime context to resolve.

## Node types

| Type | Id form |
| --- | --- |
| `module` | `module:<name>` |
| `instance` | `inst:<top>/<dot.path>` |
| `port` | `port:<module>.<port>` |
| `parameter` | `param:<module>.<name>` |
| `interface`, `modport` | `iface:<name>`, `modport:<interface>.<name>` |
| `suite` | `suite:<suite dir>` |
| `test` | `test:<suite dir>#<name>` |
| `testbench` | `tb:<suite dir>#<name>` |
| `model` | `model:<models.yaml>#<name>` |
| `spec_block` | `spec:<block>` |
| `coverage_item` | `covitem:<block>#<id>` |
| `spec_doc`, `golden_model` | `doc:<path>`, `golden:<path>` |
| `python_module` | Normally `py:<path>`; an extractor may supply another id for the same file. |

## Edge types

Design edges are `instantiates`, `child_of`, `instance_of`, `connects`, `implements` and `overrides`. Config and cross-tier edges:

| Edge | Relationship |
| --- | --- |
| `declares` | Suite to test/testbench, or spec block to coverage item. |
| `runs_on` | Test to testbench. |
| `exercises` | Testbench or non-simulation run to model. |
| `covers` | Test or formal run to coverage item. |
| `specified_by`, `documented_by`, `implements` | Model, spec block, document, and golden-model traceability. |
| `maps_to` | Model declaration to design module. |
| `elaborates_as` | Testbench to its elaborated top module. |
| `targets` | Non-simulation run to its top module. |

Binding edges are `binds_to`, `imports`, `drives`, `checks_against` and `implemented_by`. Only `drives` and `implemented_by` can be `INFERRED`. Unresolved signal or symbol matches carry `resolved: false`, and a `via` field names evidence inherited through a helper module.

Config-only graphs have dangling endpoints on purpose: config-to-design edges name modules that the design tier supplies when it is included.

## Flow provenance

The config tier reads the same loaders as the test, spec and regression commands. Flow ownership comes from each flow's regression manifest, first at the project root and then at the path in [`cfg-rtl-reg`](../reference/yaml.md#root_configyaml). A missing manifest means the project does not use that flow; an invalid one is reported. Unclaimed `tests.yaml` suites default to `sim`.

## Output paths

```text
artefacts/graph/
├── graph.json
├── graph-meta.json
├── results-overlay.json
├── design/<model>/graph.json
├── config/graph.json
├── binding/graph.json
└── bind/graph.json
```

`graph-meta.json` records the build fingerprint, input hashes, tool versions, tier status, failures, skipped items, stitch points, dangling targets and id collisions. Per-testbench and per-run design exports are nested under `design/<model>/tb/` and `design/<model>/run/`; their filelists and renderer logs are under `artefacts/hier/`.

A `--run-tag` run writes only `results-overlay.json`, under `artefacts/.runs/<tag>/graph/`.

Volatile results, seeds, timestamps and artefact paths belong only in the overlay. Consumers may join them in memory but must not write the annotated document over `graph.json`.

## Looking at the Graph

Serve the graph pane through the hub and open `http://127.0.0.1:<http_port>/gph`:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

The pane rereads the graph and overlay on reload and groups nodes by specification, design and verification flow. It can tint design nodes with joined coverage, or module nodes with physical area and power (see [Physical Heat on the Graph](#physical-heat-on-the-graph)). Clicking a node focuses the schematic or opens the source in a connected editor.

Drive the pane from a script; the hub caches the focus and delivers it when the pane connects:

```bash
rb hub send graph-focus module:dma_engine
```

See [Hub](hub.md#design-knowledge-graph-pane) for pane behavior.

## The MCP Server

Install the optional dependency, list the tools, and configure the agent host to launch the stateless stdio server:

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

Graph, test-status, coverage, physical-metrics and hierarchy tools mirror their `rb --machine` payloads. Each call rereads the artefact files, so there is no daemon and updates show up without restarting the server.

When a live hub is discoverable, the server also offers tools for hub state, selection, source opening, coordinate resolution, diagnostics, and coverage and physical focus. Without a hub those tools are omitted.

## Load graph.json directly

The file is plain JSON; NetworkX is optional:

```python
import json
import networkx as nx

with open("artefacts/graph/graph.json") as handle:
    data = json.load(handle)

graph = nx.node_link_graph(data, edges="links")
```
