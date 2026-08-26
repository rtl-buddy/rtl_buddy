## Use the MCP server

`rb mcp` exposes graph, coverage, hierarchy, and available live-hub operations as MCP tools over stdio:

```json
{"mcpServers": {"rtl-buddy": {"command": "rb", "args": ["mcp"]}}}
```

Install the optional SDK first:

```bash
uv add "rtl_buddy[mcp]"
```

Each response wraps the corresponding `--machine` payload in `{tool, ok, meta, payload}`. Command-level failures return `ok: false` and an `error`; they do not become transport failures. The CLI provides the same operations when MCP is unavailable.
