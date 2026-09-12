---
name: rtl-buddy-graph
description: Query rtl_buddy graphs, hierarchy, sources, results, hub, or MCP when an RTL question crosses files or project relationships.
---

# rtl_buddy graph and hierarchy

Report `rb --version` at the top of every run summary.

Use the graph for relationships that grep cannot compute; use ordinary file reads
for one-file facts and small config enumerations. Details and vocabulary:
`rb --machine docs show concepts/graph` and `concepts/hier`.

## Locate, then cite

```bash
rb --machine graph build
rb --machine graph results
rb --machine graph query "which tests cover ITEM"
rb --machine graph explain module:my_block
rb --machine graph path NODE_A NODE_B
```

- Rebuild after source/YAML changes; refresh results after a regression.
- `graph build` exits 1 for a failed required tier (or any degraded tier under
  `--strict`); `graph results` exits 1 for incomplete results under `--strict`.
  Otherwise they exit 0, and fatal errors exit 2. `hier`/`hier-query` propagate
  the renderer's exit code.
- A design-tier row naming a model that cannot elaborate (an SV `interface`
  library entry, a vendored filelist) is fixed in `models.yaml`, not by rerunning:
  `top:` names the real root module, `graph: false` opts the model out and lists
  it as *skipped* instead of *failed*.
- Query matching is deterministic. A no-match exit 1 is a valid empty answer;
  exit 2 usually means the graph has not been built.
- Prefer lean neighbours; use `--expand` only when full peer summaries are needed.
- Instance results carry a `cite` command. Use
  `rb hier-query <model> source-snippet <path>` to quote the source.
- Use direct file reads for port lists, one instance's connections, or a compact
  `tests.yaml`; graph payloads can cost more than those files.

`rb mcp` exposes the same query and hierarchy payloads over stdio, plus the
coverage (`cov_summary`, `cov_module`) and physical-metrics (`phys_summary`,
`phys_module`, `phys_instance`) families, which read artefacts already on disk
and run no EDA tool. It is a convenience surface, not a prerequisite; the
`--machine` CLI remains complete.

`phys_module` joins the two halves of the physical model on their `module`
column, and the two spell it differently: RTL module names in the synthesis
half, Liberty cell names (`DFF_X1`) on the power half's leaves. So it answers
"how much do the DFFs burn" and a flat netlist's top, and its payload's
`instance_join` says so when an RTL module matched no instance. Do not report
an empty instance list as "this block burns no power"; read
`rb --machine docs show concepts/phys` first.

For interactive graph, coverage, source, and waveform coordination, read
`rb --machine docs show concepts/hub` before sending hub commands.
