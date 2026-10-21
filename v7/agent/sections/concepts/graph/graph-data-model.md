## Graph data model

`graph.json` is a directed [NetworkX node-link](https://networkx.org/documentation/stable/reference/readwrite/json_graph.html) multigraph. Every node has `id`, `type`, `label` and `tier`; every edge has `type` and `confidence`. Paths in ids are project-relative. When different testbench files declare the same module name, testbench ids get an `@<suite dir>` qualifier; use the full id when a label is ambiguous.

| Tier | Contents |
| --- | --- |
| `design` | Modules, instances, ports, parameters, interfaces, and modports from `rtl-buddy-view`. |
| `config` | Suites, tests, testbenches, flow runs, models, specs, coverage items, docs, and golden models. |
| `binding` | Python modules, imports, cocotb-to-DUT and signal bindings, golden-model checks, and DPI implementations. |

Node ids you pass to `path` and `explain`: `module:<name>`, `inst:<top>/<dot.path>`, `port:<module>.<port>`, `test:<suite dir>#<name>`, `tb:<suite dir>#<name>`, `model:<models.yaml>#<name>`, `spec:<block>`, `covitem:<block>#<id>`, `py:<path>`. Use `explain` on a node to see its edges.

Edges relate a suite to its tests, a test to its testbench and coverage items, a testbench or run to its model and top module, a spec block to its coverage items, and a Python module to the DUT signals it drives or checks. `drives` and `implemented_by` can be `INFERRED`; an unresolved match has `resolved: false`.

Results, seeds and timestamps live only in the overlay. `graph.json` is plain JSON; NetworkX is optional:

```python
import json
import networkx as nx

with open("artefacts/graph/graph.json") as handle:
    graph = nx.node_link_graph(json.load(handle), edges="links")
```
