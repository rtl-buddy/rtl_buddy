## Query saved coverage with `rb cov`

`rb cov` reads existing artefacts and writes nothing. Without `--cov-dir`, it selects the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary
rb cov summary --limit 0
rb cov module blk
rb cov module blk --all
rb cov summary --cov-dir verif/blk/cov_dir
```

- `summary` reports run and test totals plus the coldest files. `--limit 0` shows all files.
- `module` reports points for exactly the recorded module. `--all` includes hit points as well as misses.

An unknown module exits 2 and reports close candidates. A file shared by modules is filtered to the requested module's points.

Machine payloads include the manifest, run metadata, totals, artefact paths, and verb-specific file, module, test, and point data. The same artefact block is included in machine output from the producing `test` or `regression` command.

`rb mcp` exposes the same query builders as `cov_summary` and `cov_module`. They read files directly and do not require a running hub. See [The MCP server](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/#the-mcp-server).
