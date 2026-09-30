## Use phys from MCP

`rb mcp` exposes `phys_runs`, `phys_summary`, `phys_module` and `phys_instance`. They read files directly and need no EDA tool and no running hub. See [The MCP server](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/graph/#the-mcp-server).

- `phys_runs` takes `limit` and is where the `phys_dir` for the other tools comes from.
- `phys_dir` and `manifest` are the tool arguments for `--phys-dir` and `--manifest`; a relative path resolves against the project root.
- All four take `limit` with the same default as the CLI flag (the head of the ranking); `0` returns every row. `phys_summary` also takes `modules_limit` and `instances_limit`, an integer or `"none"`.
- A `phys_focus` tool mirroring `rb hub send phys-focus` joins them when a live hub is discovered.
