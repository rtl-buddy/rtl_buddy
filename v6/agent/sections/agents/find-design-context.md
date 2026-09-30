## Find design context

Use the [design knowledge graph](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/) for relationships that need elaboration or cross config boundaries. Rebuild it after source or config changes, and refresh results after a regression:

```bash
rb --machine graph build
rb --machine graph results
rb --machine graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb --machine graph explain test:verif/demo_tiny_alu#flags
rb --machine graph path cocotb_random module:demo_tiny_alu
```

Use the graph to locate a source, then cite it with the returned `cite` information or:

```bash
rb hier-query <model> source-snippet <instance-path>
```

- A query exits 1 when nothing matches and 2 when no graph exists.
- Request `--expand` only when the lean peer summaries are not enough; full node expansion costs more.
- For a single-file question, or when the relevant config is smaller than a graph response, read the file directly.
