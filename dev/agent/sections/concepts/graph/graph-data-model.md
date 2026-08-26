## Graph data model

`graph.json` is directed, multigraph [NetworkX node-link JSON](https://networkx.org/documentation/stable/reference/readwrite/json_graph.html). Every node has `id`, `type`, `label`, and `tier`; source-backed nodes also carry a project-relative `file`. Every edge has `type` and `confidence`.

The tiers share one id namespace and merge by node id:

| Tier | Contents |
| --- | --- |
| `design` | Modules, instances, ports, parameters, interfaces, and modports from `rtl-buddy-view`. |
| `config` | Suites, tests, testbenches, flow runs, models, specs, coverage items, docs, and golden models. |
| `binding` | Python modules, imports, cocotb-to-DUT and signal bindings, golden-model checks, and DPI implementations. |

Paths inside ids are project-relative and POSIX-separated. When different testbench files declare the same module name, testbench-side ids are qualified with `@<suite dir>`; DUT ids remain unqualified so the tiers can stitch through them. Use the full qualified id when a label is ambiguous.
