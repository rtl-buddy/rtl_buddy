## The MCP Server

Install the optional MCP dependency and configure an agent host to launch the stateless stdio server:

```bash
uv add 'rtl_buddy[mcp]'
rb mcp --list-tools
```

```json
{
  "mcpServers": {
    "rtl-buddy": {"command": "rb", "args": ["mcp"]}
  }
}
```

Graph, test-status, coverage, and hierarchy tools mirror their `rb --machine` payloads. Each call rereads graph and coverage files, so no daemon is required and updates are visible without restarting the MCP server.

When a live hub is discoverable, the server also advertises tools for hub state, selection, source opening, coordinate resolution, diagnostics, and coverage focus. Without a hub those tools are omitted rather than exposed in a permanently failing state.
