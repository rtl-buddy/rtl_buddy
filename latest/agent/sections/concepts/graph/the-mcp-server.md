## The MCP Server

Install the optional dependency, list the tools, and configure the agent host to launch the stdio server:

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

Graph, test-status, coverage, physical-metrics and hierarchy tools mirror their `rb --machine` payloads. Each call rereads the artefact files, so updates show up without restarting the server. When a live hub is discoverable, the server also offers tools for hub state, selection, source opening, diagnostics, and coverage and physical focus; without a hub they are omitted.
