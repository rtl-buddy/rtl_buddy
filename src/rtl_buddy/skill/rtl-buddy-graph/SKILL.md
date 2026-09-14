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
half, Liberty cell names (`DFF_X1`) on the power half's leaves. An RTL module
name still gets its synthesis row — the cells and the area are measured for it
— but the *power attribution* is that join, so power and instances answer
Liberty-cell questions — "how much do the DFFs burn" — and only those: the
join matches the power half's `module` field as it stands, so no leaf row
carries an RTL module's name, the top's included, and flattening the design
changes the hierarchy rather than the namespace. Its payload's `instance_join`
says so when an RTL module matched no instance, and says it the other way when
one name is in both namespaces. Do not report an empty instance list as "this
block burns no power", and do not read it back onto the cells and area, which
stand; read `rb --machine docs show concepts/phys` first.

`phys_module` and `phys_instance` head their lists like the CLI verbs do:
`limit` defaults to the top rows and `limit: 0` asks for the complete one. The
counts beside them (`instance_count`, `child_count`) and the sums (`power`,
`rollup`) always cover every matching row, so a headed list is never a smaller
total.

`phys_focus` is served only when a live hub was discovered at start-up — with
no hub running it is absent, not failing, and the three read tools answer
without one. It points the hub's `/phy` pane at `module:<name>` or
`instance:<path>` (an unprefixed string is read as an instance path), so the
row you are discussing is the row on the user's screen; use the names the read
tools returned, and for an instance one that names a row — the pane resolves
exact leaf rows only, so `phys_instance`'s echoed path is focusable when its
`match` is `exact` and not when it is `prefix`, where the `children` entries
are the rows. A target the model does not contain is a soft miss, and the
hub replays the latest focus to a pane that connects later, so sending it
before the tab is open works.

For interactive graph, coverage, source, and waveform coordination, read
`rb --machine docs show concepts/hub` before sending hub commands.
