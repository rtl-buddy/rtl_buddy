---
description: Install the bundled agent skills, query version-matched local docs, and consume RTL Buddy's machine-readable command, graph, and log interfaces.
---

# Agent use of rtl-buddy

Agents should prefer RTL Buddy's local docs and structured command surfaces over parsing terminal formatting or searching the repository without context.

## Find design context

Build the [design knowledge graph](concepts/graph.md) after source or config changes, refresh its results after a regression, and use it for relationships that require elaboration or cross config boundaries:

```bash
rb --machine graph build
rb --machine graph results
rb --machine graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb --machine graph explain test:verif/demo_tiny_alu#flags
rb --machine graph path cocotb_random module:demo_tiny_alu
```

Use the graph to locate a source, then cite the exact implementation with the returned `cite` information or:

```bash
rb hier-query <model> source-snippet <instance-path>
```

A query exits 1 when nothing matches and 2 when no graph exists. Full node expansion costs more; request `--expand` only when the lean peer summaries are insufficient. Read files directly for single-file questions or when the relevant config is smaller than a graph response.

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

## Bundled agent skills

The wheel includes a version-matched skill family for Claude Code and Codex. The primary `rtl-buddy` skill routes advanced work to focused test, dispatch, graph, formal, and implementation skills.

```bash
rb skill install
rb skill status
rb skill uninstall
```

Install scope determines the target:

| Scope | Claude Code | Codex |
| --- | --- | --- |
| User (default) | `~/.claude/skills/<member>/SKILL.md` | `~/.codex/skills/<member>/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/<member>/SKILL.md` | `<root>/.agents/skills/<member>/SKILL.md` |
| Explicit dir (`--dir PATH`) | `<PATH>/<member>/SKILL.md` | — |

`<member>` is `rtl-buddy`, `rtl-buddy-test`, `rtl-buddy-dispatch`, `rtl-buddy-graph`, `rtl-buddy-fpv`, or `rtl-buddy-implementation`.

Use project scope only to override user-level skills for a project pinned to a different major. Project discovery walks up for `root_config.yaml`, then `.git/`. Use `--dir PATH` for a flat family outside the normal layout; it cannot be combined with `--project` or `--root`.

Re-run installation after upgrading. It refreshes every member and removes obsolete skill directories at the selected scope. Install or uninstall once at every scope you use. Project installation updates `.gitignore`; pass `--no-gitignore` to suppress that edit.

## Local docs access

The wheel includes the docs for its installed version:

```bash
rb docs list
rb docs show agents
rb docs show concepts/tests#interpret-results
rb --machine docs list
rb --machine docs show reference/yaml
```

`docs list` returns each page's slug, title, and frontmatter description. `docs show` accepts a slug and optional section anchor.

In machine mode, `docs list` uses the standard command envelope. `docs show` is the exception: it prints the page payload as a bare JSON object so consumers can use the content directly.

Every published Docusaurus version also exposes a static agent surface. Use
`llms.txt` for discovery, `agent/catalog.json` for structured page and section
metadata, `agent/pages/<slug>.md` for a raw page, or
`agent/sections/<slug>/<anchor>.md` for one bounded section. These URLs are
versioned under `dev/` or `v<major>/`; they are the network-accessible mirror,
not a replacement for the installed-version and offline guarantees above.

## Machine mode

Pass `--machine` before the subcommand:

```bash
rb --machine test basic
rb --machine regression -c regression.yaml
```

Machine mode:

- writes `rtl_buddy.log` as JSON Lines;
- disables Rich formatting, colors, and spinners;
- prints one structured JSON result to stdout for supported commands;
- captures Python hook stdout as `hook.stdout` events so it cannot corrupt the result.

A hook that starts an external process inheriting file descriptor 1 can still write to stdout. Redirect that process explicitly; see [Hook execution context](concepts/plugins.md#handle-hook-execution-context).

## Parse command results

Structured commands emit this top-level shape:

```json
{
  "command": "test",
  "exit_code": 0,
  "meta": {
    "rtl_buddy_version": "6.40.0",
    "argv": ["rb", "--machine", "test", "basic"],
    "cwd": "/path/to/suite",
    "git": {"branch": "main", "commit": "abc1234", "modified": 0, "staged": 0}
  },
  "payload": {
    "results": [{"name": "basic", "result": "PASS", "desc": "basic completed"}]
  }
}
```

Parse the whole stdout value with `json.loads()`. The stable top-level fields are `command`, `exit_code`, `meta`, and command-specific `payload`. Optional fields may be added under `meta` or `payload`; incompatible changes require a major version change.

Common payload conventions:

- listing commands use `payload.names`;
- regression results use `payload.results` and include `suite`;
- `docs list` uses `payload.pages`;
- coverage and formal commands attach structured metrics and artefact paths to their results.

Use [Coverage](concepts/coverage.md) and [Formal Property Verification](concepts/fpv.md) for their payload-specific contracts. Use [Tests](concepts/tests.md#interpret-results) for status and exit-code semantics.

## Read event logs

Each line in a machine-mode `rtl_buddy.log` is one JSON event:

```json
{"event":"sim.completed","test":"smoke","duration_sec":4.2,"message":"smoke: simulation completed in 4.20s"}
{"event":"postproc.completed","test":"smoke","result":"PASS","desc":"smoke completed","message":"smoke: post-processing completed with result PASS"}
```

Use `event` as the discriminator and consume event-specific fields rather than parsing `message`. For a test, `postproc.completed.result` and `.desc` are authoritative. Multi-suite runs also write a log in each suite directory.
