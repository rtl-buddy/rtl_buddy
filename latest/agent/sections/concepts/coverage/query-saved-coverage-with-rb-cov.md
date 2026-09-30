## Query saved coverage with `rb cov`

`rb cov` reads existing artefacts and writes nothing. Without `--cov-dir` it uses the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary
rb cov summary --limit 0
rb cov summary --by-source
rb cov summary --cov-dir verif/blk/cov_dir
rb cov module blk
rb cov module blk --all
```

- `summary` reports run and test totals and the coldest files. `--limit 0` shows all files. `--by-source` ranks files by source point.
- `module` reports the points of exactly the named module, and `--all` includes hit points as well as misses. Module figures are per elaboration.

An unknown module exits 2 and lists close candidates. With `--machine`, both totals blocks are always present. `rb mcp` exposes the same data as `cov_summary` and `cov_module`, with no hub needed; see [The MCP server](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/graph/#the-mcp-server).
