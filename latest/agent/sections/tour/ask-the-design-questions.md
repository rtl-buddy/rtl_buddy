## Ask the design questions

Everything above also answers in JSON. `--machine` turns any command's console into a structured payload, `rb hier-query` and `rb graph` answer questions about the design directly, and `rb docs` serves this documentation offline for the installed version.

```bash
cd verif/demo_tiny_alu
uv run rb --machine test basic
```

```json
{"command": "test", "exit_code": 0, "payload": {"results": [{"name": "basic", "result": "PASS", "desc": "(nwrn=  0)"}]}}
```

```bash
cd design/demo_tiny_alu_subsys
uv run rb --machine hier-query demo_tiny_alu_subsys_top instances-of ip_cdc_handshake
```

```json
[{"instance_path": "demo_tiny_alu_subsys_top.u_hs_cmd", "module_name": "ip_cdc_handshake",
  "param_overrides": [{"param_name": "WIDTH", "value_text": "19",
    "location": {"file": ".../demo_tiny_alu_subsys_top.sv", "start_line": 92}}],
  "location": {"file": ".../design/common/ip_cdc_handshake.sv", "start_line": 28, "end_line": 96}},
 {"instance_path": "demo_tiny_alu_subsys_top.u_hs_result", "module_name": "ip_cdc_handshake",
  "param_overrides": [{"param_name": "WIDTH", "value_text": "12", "...": "..."}]}]
```

The design knowledge graph joins specs, RTL, tests, and coverage, so a question that crosses those boundaries is one query:

```bash
uv run rb graph build
uv run rb graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
```

```text
covitem:demo_tiny_alu#SAND-FUNC-FLAG-C-ADD
  <- declares spec:demo_tiny_alu
  <- covers test:verif/demo_tiny_alu#flags
  <- covers test:verif/demo_tiny_alu_cocotb#cocotb_flags
```

The graph was built from this project in a few seconds: 800 nodes and 1619 links across RTL, config, spec, and cocotb binding tiers. `rb mcp` exposes the same graph, coverage, physical-metrics, and hierarchy queries as MCP tools, and `rb skill install` gives Claude Code or Codex a version-matched skill that knows every command on this page. See [Agent use of rtl-buddy](https://rtl-buddy.github.io/rtl_buddy/v8/agents/) and [Design Knowledge Graph](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/graph/).
