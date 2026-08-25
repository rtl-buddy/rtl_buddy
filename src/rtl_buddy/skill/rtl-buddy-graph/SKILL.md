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
- Query matching is deterministic. A no-match exit 1 is a valid empty answer;
  exit 2 usually means the graph has not been built.
- Prefer lean neighbours; use `--expand` only when full peer summaries are needed.
- Instance results carry a `cite` command. Use
  `rb hier-query <model> source-snippet <path>` to quote the source.
- Use direct file reads for port lists, one instance's connections, or a compact
  `tests.yaml`; graph payloads can cost more than those files.

`rb mcp` exposes the same query and hierarchy payloads over stdio. It is a
convenience surface, not a prerequisite; the `--machine` CLI remains complete.

For interactive graph, coverage, source, and waveform coordination, read
`rb --machine docs show concepts/hub` before sending hub commands.
