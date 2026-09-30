## Query the graph

Run the read verbs from the project root:

```bash
rb graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb graph path cocotb_random module:demo_tiny_alu
rb graph explain test:verif/demo_tiny_alu#flags
```

- `query` does keyword matching with bounded neighbourhood expansion. Narrow it with `--type`, `--tier`, `--depth` or `--limit`.
- `path` returns shortest paths. Traversal is undirected by default, because edge direction expresses role, not reachability. Pass `--directed` when direction matters.
- `explain` returns one node's attributes, edges, test result, coverage entry, and, for instance nodes, a command that cites the source.

A bare name works only when it identifies one node; otherwise the command fails with the candidate ids. `query` exits 1 when nothing matches, and an invalid or ambiguous node reference exits 2. The verbs can run during a regression. Pass `--no-results` for a structural-only answer.

With `--machine` each verb emits the standard [machine envelope](https://rtl-buddy.github.io/rtl_buddy/v6/agents/#machine-mode). Truncation metadata reports neighbours cut off by bounded expansion; raise the limit or explain a specific peer rather than assuming the result is complete.
