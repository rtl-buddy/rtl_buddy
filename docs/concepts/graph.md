---
description: Build and query rtl_buddy's design knowledge graph, join test and coverage results, and use its CLI, MCP, and browser interfaces.
---

# Design Knowledge Graph

The design knowledge graph connects project configuration, elaborated RTL hierarchy, tests, specifications, and source bindings. Use it for questions that cross files or require elaborated relationships; read a source or config file directly for a fact contained in one file.

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

Useful reductions are `--no-design`, `--no-tb`, `--no-flow-tops`, `--no-bind`, and `--no-extract`. Use `--force` to ignore a valid cache. See the [CLI reference](../reference/cli.md#graph) for the complete option list.

The design tier requires a compatible `rtl-buddy-view`:

```bash
rb tool-check --explain rtl-buddy-view
```

The external extractor is optional. If absent, its tier is `skipped` and the graph remains usable. A requested tier that breaks is `failed`; per-model failures become non-zero only with `--strict`. Inspect `graph-meta.json` for tier status and failure details.

## Models with no elaborable root

The design tier roots each model's export at the model's `top:`, defaulting to the model name. A model that has no such module — an SV `interface` published as a library entry, or a filelist of vendored IP — sets `graph: false` in [`models.yaml`](../reference/yaml.md#modelsyaml).

An opted-out model, and any testbench or non-simulation run rooted at it, is listed under the design tier's `skipped` entries in the envelope and in `graph-meta.json`, never under `failures`. `skipped` does not affect the exit code, including under `--strict`. If every model in scope opts out, the tier itself is `skipped`.

The config tier still emits the model node, carrying `graph: false`, so `spec:` and test cross-references resolve. It emits no `maps_to` edge for it, and no `elaborates_as` or `targets` edge from a testbench or run over it, because the design tier will not define those `module:` nodes. A testbench is judged by the models its tests name, not by the module it tops at, so a graphable model's testbench keeps its edge even when the two models share a root module. Give the model a `top:` instead when the filelist elaborates and only the root module name differs.

The opt-out is design-tier-only. `rb hier`, `rb hier-query`, and `rb axi-profile` still elaborate the model and still fail if its root does not resolve.

Every model the build selects must root at a distinct module. `module:<top>` is a global id and DUT ids are never suite-qualified, so two exports sharing a top merge into one hybrid hierarchy instead of staying apart. `graph build` refuses that input before invoking the exporter and names both models, both `models.yaml` files and the shared top; `graph: false` on one of them is the documented way out.

Unchanged inputs, tool versions, and schema produce a cached no-op build. A failure remains cached until an input or tool version changes, or `--force` is used.

## Query the graph

Use the three read verbs from the project root:

```bash
rb graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb graph path cocotb_random module:demo_tiny_alu
rb graph explain test:verif/demo_tiny_alu#flags
```

- `query` performs deterministic keyword matching and bounded neighbourhood expansion. Use `--type`, `--tier`, `--depth`, or `--limit` to narrow the result.
- `path` returns shortest paths. Traversal is undirected by default because edge direction expresses role, not reachability; pass `--directed` when direction matters.
- `explain` returns one node's attributes, incident edges, test result, coverage entry, and a source-citation command for instance nodes.

Bare names are accepted only when they identify one node. Ambiguous names fail with candidate ids instead of choosing silently. `query` exits 1 when nothing matches; an invalid or ambiguous node reference exits 2.

All three verbs read without taking the graph write lock, so they can run while a regression is writing separate test artefacts. Pass `--no-results` for a structural-only query.

With `--machine`, each command emits the standard [machine envelope](../agents.md#machine-mode). Query payloads include the graph and overlay paths plus `matches`, `paths`, or the explained node. Truncation metadata reports neighbours omitted by bounded expansion; raise the corresponding limit or explain a specific peer rather than assuming the result is complete.

## Choose graph or source lookup

The graph is most useful for:

- transitive impact through elaborated hierarchy;
- paths that cross design, test, specification, and binding data;
- stable node ids and exact structural relationships;
- joining current results or coverage to those relationships.

For a port list, a YAML field, or another single-file fact, use the graph to locate the source and read the relevant lines. Do not enumerate a small config file through many `explain` calls. Use `explain --expand` only when the full attributes of every peer are needed.

`scripts/graph_token_benchmark.py` compares graph and raw-file routes on a built project. Run it after changing query payloads:

```bash
uv run python scripts/graph_token_benchmark.py -p /path/to/project --markdown
```

## Results Overlay

`rb graph results` writes current run state to `artefacts/graph/results-overlay.json` without modifying structural `graph.json`:

```bash
rb graph results
rb graph results --strict
```

Entries are keyed by `test:<suite dir>#<test name>` and contain the latest result status, seed, timestamp, and paths to artefacts that exist. The timestamp is the result envelope's file modification time, so refreshing unchanged inputs is byte-stable.

An entry also carries an optional `compile` block with `duration_sec`, `builder`, and `reused` when the run's result envelope records one. A local run records its own. A dispatched run produces two envelopes that disagree on purpose: the simulation job writes `artefacts/<test>/result.json` with its own `compile` block, which says `reused: true` and near-zero duration because the shared build already produced the `simv`, and at collect the head overwrites the `compile` block in `artefacts/<test>/dispatch/result-<tag>.json` with the build job's record. The overlay takes the newest envelope, which is the head's, so a dispatched run reports the shared build's compile — the one that did the work. The exception is a simulation job that had to rebuild because the prebuilt stamp did not validate (`compile.prebuilt_stamp_invalid`): the overlay still reports the build job's record, not the recompile that actually produced that run's `simv`. Its values are read from the envelope, never measured at overlay time, and the block is absent when the envelope says nothing about the compile, so byte-stability holds for a refresh with nothing rerun.

Result status comes from each run's `result.json`, not from log parsing. A test directory with artefacts but no result envelope is retained as `UNKNOWN`. Random-test iterations remain available under `runs`; the newest iteration supplies the entry's top-level status.

When cross-checking against `graph.json`:

- `missing` identifies graph test nodes with no result.
- `unmatched` identifies results with no declared test node, such as generated sweep names.
- `problems` identifies unreadable result data.

These are reported normally and become failures with `--strict`. A missing or unreadable overlay does not prevent structural queries.

## Coverage on the Graph

`rb graph results` can correlate declared `covers:` relationships with observed coverage already on disk. It never reruns the simulator or rewrites `graph.json`.

The default `--coverage auto` uses the newest coverage manifest and model, then falls back to per-test `coverage.dat` files. You can select a manifest with `--cov-dir` or `--cov-manifest`, pass a merged LCOV `.info` file, require the model source with `--coverage model`, or disable the join with `--no-coverage`.

Coverage items receive one of three states:

| State | Meaning |
| --- | --- |
| `exercised` | A declared item matched an observed cover point with hits. |
| `declared-only` | The item was declared but no matched point fired. |
| `observed-but-undeclared` | An observed cover point has no `covers:` declaration. |

Name correlation prefers exact, case-insensitive, normalized, then `cov`/`cvr`/`c`-affix matches. The selected rung is recorded; treat an `affix` match as a prompt to align the names. Module coverage joins exact elaborated names first, then names with one trailing parameterization suffix removed. Multiple elaborations are aggregated onto the source-module node.

LCOV lacks module and per-test identity. It joins design coverage by resolved file and still uses any available per-test databases for test badges and coverage-item verdicts. Unresolved, re-anchored, or unmatched paths are reported rather than guessed.

See [Coverage](coverage.md) for metric semantics and coverage collection.

## Graph data model

`graph.json` is directed, multigraph [NetworkX node-link JSON](https://networkx.org/documentation/stable/reference/readwrite/json_graph.html). Every node has `id`, `type`, `label`, and `tier`; source-backed nodes also carry a project-relative `file`. Every edge has `type` and `confidence`.

The tiers share one id namespace and merge by node id:

| Tier | Contents |
| --- | --- |
| `design` | Modules, instances, ports, parameters, interfaces, and modports from `rtl-buddy-view`. |
| `config` | Suites, tests, testbenches, flow runs, models, specs, coverage items, docs, and golden models. |
| `binding` | Python modules, imports, cocotb-to-DUT and signal bindings, golden-model checks, and DPI implementations. |

Paths inside ids are project-relative and POSIX-separated. When different testbench files declare the same module name, testbench-side ids are qualified with `@<suite dir>`; DUT ids remain unqualified so the tiers can stitch through them. Use the full qualified id when a label is ambiguous.

## Node Types

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

`reglvl` is stored as written because resolving builder-specific values requires runtime context.

## Edge Types

Design edges are `instantiates`, `child_of`, `instance_of`, `connects`, `implements`, and `overrides`.

Important cross-tier and config edges are:

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

Binding edges are `binds_to`, `imports`, `drives`, `checks_against`, and `implemented_by`. Only `drives` and `implemented_by` may be `INFERRED`; unresolved signal or symbol matches carry `resolved: false`. A `via` field identifies evidence inherited through a helper module.

The config tier reads the same loaders as the test, spec, and regression commands.

## Flow provenance

Flow ownership comes from each flow's regression manifest, first at the project root and then at the path configured in [`cfg-rtl-reg`](../reference/yaml.md#root_configyaml). A missing manifest means the project does not use that flow; an invalid manifest is reported. Unclaimed `tests.yaml` suites default to `sim`.

## Output Paths

The stable outputs are:

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

`graph-meta.json` records the build fingerprint, input hashes, tool versions, tier status, failures, skipped items, stitch points, dangling targets, and id collisions. Per-testbench and per-run design exports are nested under `design/<model>/tb/` and `design/<model>/run/`. Their generated filelists and renderer logs live under `artefacts/hier/`.

Volatile results, seeds, timestamps, and artefact paths belong only in the overlay. Consumers may join them in memory but must not write the annotated document over `graph.json`.

## Looking at the Graph

Serve the interactive graph pane through the hub:

```bash
rb graph build
rb graph results
rb hub start --serve-viewer
```

Open `http://127.0.0.1:<http_port>/gph`. The pane reads the graph and overlay on reload, groups nodes by specification, design, and verification flow, and can tint design nodes with joined coverage.

Node clicks can focus the schematic or open source in a connected editor. Drive the pane from a script with:

```bash
rb hub send graph-focus module:dma_engine
```

The hub caches the focus so it is delivered when the pane connects. See [Hub](hub.md#design-knowledge-graph-pane) for browser behavior.

## The MCP Server

Install the optional MCP dependency and configure an agent host to launch the stateless stdio server:

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

Graph, test-status, coverage, and hierarchy tools mirror their `rb --machine` payloads. Each call rereads graph and coverage files, so no daemon is required and updates are visible without restarting the MCP server.

When a live hub is discoverable, the server also advertises tools for hub state, selection, source opening, coordinate resolution, diagnostics, and coverage focus. Without a hub those tools are omitted rather than exposed in a permanently failing state.

## Load graph.json directly

The file is plain JSON; NetworkX is optional:

```python
import json
import networkx as nx

with open("artefacts/graph/graph.json") as handle:
    data = json.load(handle)

graph = nx.node_link_graph(data, edges="links")
```

Do not assume that a config-only graph has no dangling endpoints: config-to-design edges intentionally name modules that the design tier supplies when included.
