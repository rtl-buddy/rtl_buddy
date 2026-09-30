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
