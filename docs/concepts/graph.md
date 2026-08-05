---
description: The design knowledge graph — the shared graph.json contract, the node and edge vocabulary of each tier, and how rtl_buddy extracts the config tier from tests.yaml, models.yaml and specs.yaml.
---

# Design Knowledge Graph

The design knowledge graph is one JSON file describing how a project fits together: which test runs on which testbench, which testbench exercises which model, which module that model is, which spec block specifies it, and which coverage items each test claims. Agents query it instead of grepping the tree.

It is assembled from three independent tiers that share one node-id namespace:

| Tier | Produced by | Covers |
| --- | --- | --- |
| design | `rtl-buddy-view` | modules, instances, ports, parameters, interfaces |
| config | `rtl_buddy` (this page) | suites, tests, testbenches, models, spec blocks, coverage items, spec docs, golden models |
| binding | Graphify | Python-level structure of cocotb tests and golden models |

Merging is a node-id union. Identical ids emitted by different tiers are the stitch points, so each tier can be produced, cached and re-exported on its own.

## graph.json Envelope

The file is [NetworkX node-link JSON](https://networkx.org/documentation/stable/reference/readwrite/json_graph.html), so `networkx.node_link_graph(data, edges="links")` loads it directly:

```json
{
  "directed": true,
  "multigraph": true,
  "graph": {
    "schema_version": 1,
    "generator": {"tool": "rtl_buddy", "version": "6.24.0", "tier": "config"},
    "project_root_rel": "."
  },
  "nodes": [
    {"id": "test:verif/demo_tiny_alu_cocotb#cocotb_random", "type": "test", "label": "cocotb_random", "tier": "config", "file": "verif/demo_tiny_alu_cocotb/tests.yaml", "reglvl": 1000}
  ],
  "links": [
    {"source": "test:verif/demo_tiny_alu_cocotb#cocotb_random", "target": "tb:verif/demo_tiny_alu_cocotb#tb_alu_random", "type": "runs_on", "confidence": "EXTRACTED"}
  ]
}
```

Every node carries `id`, `type`, `label` and `tier`; `file` (repo-relative) is present wherever the source is a file. Every link carries `confidence`: `EXTRACTED` for anything read straight out of config or source, `INFERRED` / `AMBIGUOUS` only for the binding tier's `dut.<signal>` scan.

Paths inside ids are always repo-relative and posix-separated, so an id computed on macOS matches one computed on Linux.

## Output Paths

The merged graph lives at `<project root>/artefacts/graph/graph.json`, with provenance beside it in `graph-meta.json`. The sidecar holds the generator identity and the SHA-256 of every input file per tier — that is how a consumer knows whether a cached graph is stale.

Volatile data never enters `graph.json`: no pass/fail status, no seeds, no artefact paths, no timestamps. Results are a separate overlay file keyed by node id, so re-running a regression does not churn the graph.

## Node Types

Design tier (`rtl-buddy-view`):

| Type | Id |
| --- | --- |
| `module` | `module:<module_name>` |
| `instance` | `inst:<top>/<dot.path>` |
| `port` | `port:<module_name>.<port_name>` |
| `parameter` | `param:<module_name>.<name>` |
| `interface` | `iface:<name>` |
| `modport` | `modport:<iface>.<name>` |

Config tier (`rtl_buddy`):

| Type | Id | Notable attributes |
| --- | --- | --- |
| `suite` | `suite:<suite dir>` | |
| `test` | `test:<suite dir>#<test name>` | `reglvl` (raw, as written), `cocotb_modules`, `xfail` |
| `testbench` | `tb:<suite dir>#<tb name>` | `toplevel`, `kind` (`cocotb`/`systemc`/`hdl`) |
| `model` | `model:<models.yaml>#<name>` | `desc` |
| `spec_block` | `spec:<block name>` | `desc` |
| `coverage_item` | `covitem:<block name>#<ID>` | `desc`, `block` |
| `spec_doc` | `doc:<path>` | `exists` |
| `golden_model` | `golden:<path>` | `referenced_by` |

`reglvl` is kept exactly as written, including the per-builder dict form. Resolving it needs a builder, which is a run-time choice with no place in a static graph.

## Edge Types

Design tier: `instantiates`, `child_of`, `instance_of`, `connects`, `implements` (module to modport), `overrides`.

Config tier:

| Edge | From | To |
| --- | --- | --- |
| `declares` | suite | test, testbench |
| `declares` | spec block | coverage item |
| `runs_on` | test | testbench |
| `exercises` | testbench | model |
| `covers` | test | coverage item |
| `specified_by` | model | spec block |
| `documented_by` | spec block | spec doc |
| `implements` | golden model | spec block |
| `maps_to` | model | **design-tier** module |

Binding tier: `binds_to` (test to Python module), `drives` (Python module to port), `checks_against` (test to golden model).

`maps_to` is the config-to-design stitch. Its target `module:<name>` is a design-tier id that the config tier never creates — exporting the config tier alone leaves those targets dangling, which node-link readers resolve by auto-creating an attribute-less node. Merging with a design-tier export fills them in.

## What the Config Tier Reads

Everything comes from the loaders that back `rb test`, `rb spec check-coverage` and `rb spec check-design`; there is no second YAML parser, so the graph cannot disagree with those commands.

- `specs.yaml` under `<root>/spec` gives spec blocks, their `docs:` and their `coverage-items`.
- `models.yaml` under `<root>/design` gives models, and each model's `spec:` back-pointer gives `specified_by`.
- `tests.yaml` under `<root>/verif` gives suites, testbenches and tests. A test's `testbench:` becomes `runs_on`, its `model:` becomes `exercises` on the testbench, and each entry in its `covers:` becomes a `covers` edge.
- Golden models are found by convention: non-private Python files sitting next to a `specs.yaml`, for example `spec/demo_tiny_alu/tiny_alu_model.py`. Each node records the verif files that mention it in `referenced_by`.

A `covers:` entry names a bare coverage-item id, and more than one block may declare the same id. `rb spec check-coverage` treats every such block as covered, and the graph matches it: one `covers` edge per declaring block.

Testbenches declared but used by no test still become nodes — a dead testbench should be visible in the graph, not silently absent. Suites that fail to load are reported rather than fatal; the failure list lands in `graph-meta.json`.

## Python API

`rb graph` is not wired up yet. The extractor is importable:

```python
from rtl_buddy.graph import extract_config_tier, write_graph_json, write_graph_meta

result = extract_config_tier("/path/to/project")
write_graph_json(result.graph, "artefacts/graph/graph.json")
write_graph_meta(result.meta, "artefacts/graph/graph-meta.json")
```

`build_config_tier(project_root)` is the shorthand when only the graph dict is wanted. `spec_dir`, `verif_dir` and `design_dir` override the search roots; each defaults to the same directory the `rb spec` commands use. Node and link lists are sorted, so re-exporting an unchanged project produces a byte-identical file.
