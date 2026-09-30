## Use the MCP server

`rb mcp` exposes graph, coverage, physical-metrics, hierarchy, and live-hub operations as MCP tools over stdio. Install the optional SDK, then register the server:

```bash
uv add "rtl_buddy[mcp]"
```

```json
{"mcpServers": {"rtl-buddy": {"command": "rb", "args": ["mcp"]}}}
```

Each response wraps the matching `--machine` payload as `{tool, ok, meta, payload}`. A command-level failure returns `ok: false` with an `error`; it is not a transport failure. The CLI offers the same operations when MCP is unavailable.

The physical-metrics tools read the `phys-model.json` and `phys-manifest.json` that `rb synth` and `rb power` write. They run no EDA tool and need no hub.

- `phys_runs` lists every run under the project with its power mode, activity, and configuration fingerprint. It supplies the `phys_dir` the other three tools take.
- `phys_summary` limits both rankings to `limit`. `modules_limit` and `instances_limit` override it per ranking, and `"none"` omits a ranking.
- `phys_module` and `phys_instance` return one module or instance. The model's `module` column holds RTL module names on the synthesis side and Liberty cell names on the power side; see [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/phys/#what-the-module-join-can-answer) before attributing power to an RTL block.
- `phys_focus` is available when a live hub is discovered.
