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

With `--machine`, each command emits the standard [machine envelope](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#machine-mode). Query payloads include the graph and overlay paths plus `matches`, `paths`, or the explained node. Truncation metadata reports neighbours omitted by bounded expansion; raise the corresponding limit or explain a specific peer rather than assuming the result is complete.
