## Use the MCP server

`rb mcp` exposes graph, coverage, physical-metrics, hierarchy, and available live-hub operations as MCP tools over stdio:

```json
{"mcpServers": {"rtl-buddy": {"command": "rb", "args": ["mcp"]}}}
```

Install the optional SDK first:

```bash
uv add "rtl_buddy[mcp]"
```

Each response wraps the corresponding `--machine` payload in `{tool, ok, meta, payload}`. Command-level failures return `ok: false` and an `error`; they do not become transport failures. The CLI provides the same operations when MCP is unavailable.

The physical-metrics tools are `phys_runs`, `phys_summary`, `phys_module`, and `phys_instance`, reading the `phys-model.json` and `phys-manifest.json` that `rb synth` and `rb power` write; they run no EDA tool and need no hub. `phys_runs` lists every run under the project with the power mode, the activity and the configuration fingerprint each recorded, and is where the `phys_dir` the other three take comes from. `phys_summary` heads both its rankings at `limit`, and takes `modules_limit`/`instances_limit` to override it one ranking at a time — each accepting `"none"` for a ranking you do not want, so the complete module table need not carry every leaf instance row. `phys_focus` joins them when a live hub is discovered. `phys_module` joins on the model's `module` column, which holds RTL module names in the synthesis half and Liberty cell names in the power half — see [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#what-the-module-join-can-answer) before attributing power to an RTL block.
