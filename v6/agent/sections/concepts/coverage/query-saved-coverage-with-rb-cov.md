## Query saved coverage with `rb cov`

`rb cov` reads existing artefacts and writes nothing. Without `--cov-dir`, it selects the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary
rb cov summary --limit 0
rb cov module blk
rb cov module blk --all
rb cov summary --cov-dir verif/blk/cov_dir
rb cov summary --by-source
```

- `summary` reports run and test totals plus the coldest files. `--limit 0` shows all files. The totals table carries a `run` row and a `run (source)` row; `--by-source` reports the coldest files by source point instead of per elaboration, listing the same files in the same order.
- `module` reports points for exactly the recorded module. `--all` includes hit points as well as misses. Module figures are per elaboration by definition — a model module *is* one elaboration.

An unknown module exits 2 and reports close candidates. A file shared by modules is filtered to the requested module's points.

Machine payloads include the manifest, run metadata, totals, artefact paths, and verb-specific file, module, test, and point data. `--by-source` changes no payload: the machine payload carries both totals blocks either way. The same artefact block is included in machine output from the producing `test` or `regression` command.

`rb mcp` exposes the same query builders as `cov_summary` and `cov_module`. They read files directly and do not require a running hub. See [The MCP server](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/#the-mcp-server).
