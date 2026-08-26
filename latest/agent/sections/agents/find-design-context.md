## Find design context

Build the [design knowledge graph](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/) after source or config changes, refresh its results after a regression, and use it for relationships that require elaboration or cross config boundaries:

```bash
rb --machine graph build
rb --machine graph results
rb --machine graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb --machine graph explain test:verif/demo_tiny_alu#flags
rb --machine graph path cocotb_random module:demo_tiny_alu
```

Use the graph to locate a source, then cite the exact implementation with the returned `cite` information or:

```bash
rb hier-query <model> source-snippet <instance-path>
```

A query exits 1 when nothing matches and 2 when no graph exists. Full node expansion costs more; request `--expand` only when the lean peer summaries are insufficient. Read files directly for single-file questions or when the relevant config is smaller than a graph response.
