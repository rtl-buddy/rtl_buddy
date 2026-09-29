---
name: rtl-buddy-graph
description: Query rtl_buddy graphs, hierarchy, sources, results, hub, or MCP when an RTL question crosses files or project relationships.
---

# rtl_buddy graph and hierarchy

Report `rb --version` at the top of every run summary.

Use the graph for relationships that grep cannot compute. Read files directly for one-file facts and small config enumerations. Details: `rb --machine docs show concepts/graph` and `concepts/hier`.

## Locate, then cite

```bash
rb --machine graph build
rb --machine graph results
rb --machine graph query "which tests cover ITEM"
rb --machine graph explain module:my_block
rb --machine graph path NODE_A NODE_B
```

- Rebuild after source or YAML changes; refresh results after a regression.
- `graph build` exits 1 for a failed required tier, or any degraded tier under `--strict`. `graph results` exits 1 for incomplete results under `--strict`. Fatal errors exit 2. `hier` and `hier-query` pass through the renderer's exit code.
- A design-tier failure for a model that cannot elaborate (an SV `interface` library entry, a vendored filelist) is fixed in `models.yaml`, not by rerunning. `top:` names the real root module; `graph: false` reports the model as *skipped* instead of *failed*.
- Query matching is deterministic. Exit 1 with no match is a valid empty answer. Exit 2 usually means the graph is not built.
- Prefer lean neighbours; use `--expand` only when you need full peer summaries.
- Instance results carry a `cite` command. Quote source with `rb hier-query <model> source-snippet <path>`.
- Read files directly for port lists, one instance's connections, or a compact `tests.yaml`; graph payloads can cost more.

## MCP

`rb mcp` serves the same query and hierarchy payloads over stdio, plus coverage (`cov_summary`, `cov_module`) and physical-metrics (`phys_runs`, `phys_summary`, `phys_module`, `phys_instance`) tools. They read artefacts already on disk and run no EDA tool. MCP is optional; the `--machine` CLI is complete.

## Read `phys_module` correctly

The physical model has two halves keyed by `module`, and they spell it differently: RTL module names in the synthesis half, Liberty cell names (`DFF_X1`) in the power half.

- An RTL module name still gets its synthesis row: its cells and area are measured.
- Power attribution uses the join, so power and instances answer Liberty-cell questions ("how much do the DFFs burn") and only those. No leaf row carries an RTL module name, the top's included, and flattening the design changes the hierarchy rather than the namespace.
- `instance_join` in the payload says when an RTL module matched no instance, and when one name is in both namespaces.
- Never report an empty instance list as "this block burns no power", and do not carry it over to the cells and area, which stand.

Read `rb --machine docs show concepts/phys` first.

## Physical list limits

`phys_module` and `phys_instance` head their lists like the CLI verbs. `limit` defaults to the top rows, and `limit: 0` asks for the complete one. `instance_count`, `child_count`, `power` and `rollup` always cover every matching row, so a headed list is never a smaller total.

`phys_instance` rolls up the question its `match` names. On a `prefix` match it sums every leaf under the path. On an `exact` match it returns the named row alone, and `children` are for navigation, not for adding up.

## Point the hub with `phys_focus`

`phys_focus` is served only when a live hub was discovered at start-up. With no hub running it is absent, not failing, and the read tools work without one.

- It points the hub's `/phy` pane at `module:<name>` or `instance:<path>`; an unprefixed string is read as an instance path.
- Use names the read tools returned. For an instance, use one that names a row: the pane resolves exact leaf rows only. `phys_instance`'s echoed path is focusable when `match` is `exact`, not when it is `prefix`, where the `children` entries are the rows.
- A target the model does not contain is a soft miss.
- The hub replays the latest focus to a pane that connects later, so sending it before the tab is open works.

For interactive graph, coverage, source and waveform coordination, read `rb --machine docs show concepts/hub` before sending hub commands.
