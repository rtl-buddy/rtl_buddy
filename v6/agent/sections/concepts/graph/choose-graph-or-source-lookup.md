## Choose graph or source lookup

The graph is most useful for:

- transitive impact through elaborated hierarchy;
- paths that cross design, test, specification, and binding data;
- stable node ids and exact structural relationships;
- joining current results or coverage to those relationships.

For a port list, a YAML field, or another single-file fact, use the graph to locate the source and read the relevant lines. Do not enumerate a small config file through many `explain` calls. Use `explain --expand` only when the full attributes of every peer are needed.

`scripts/graph_token_benchmark.py` compares graph and raw-file routes on a built project. Run it after changing query payloads:

```bash
uv run python scripts/graph_token_benchmark.py -p /path/to/project --markdown
```
