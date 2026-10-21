---
description: Install the bundled agent skills, query version-matched local docs, and consume RTL Buddy's machine-readable command, graph, and log interfaces.
---

# Agent use of rtl-buddy

Use version-matched local docs and structured output to run RTL Buddy and interpret its results.

## Find design context

Use the [design knowledge graph](concepts/graph.md) for relationships that need elaboration or cross config boundaries. Rebuild it after source or config changes, and refresh results after a regression:

```bash
rb --machine graph build
rb --machine graph results
rb --machine graph query "which tests cover SAND-FUNC-FLAG-C-ADD"
rb --machine graph explain test:verif/demo_tiny_alu#flags
rb --machine graph path cocotb_random module:demo_tiny_alu
```

Use the graph to locate a source, then cite it with the returned `cite` information or:

```bash
rb hier-query <model> source-snippet <instance-path>
```

- A query exits 1 when nothing matches and 2 when no graph exists.
- Request `--expand` only when the lean peer summaries are not enough; full node expansion costs more.
- For a single-file question, or when the relevant config is smaller than a graph response, read the file directly.

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
- `phys_module` and `phys_instance` return one module or instance. The model's `module` column holds RTL module names on the synthesis side and Liberty cell names on the power side; see [Physical Metrics](concepts/phys.md#what-the-module-join-can-answer) before attributing power to an RTL block.
- `phys_focus` is available when a live hub is discovered.

## Bundled agent skills

The wheel ships a version-matched skill family for Claude Code and Codex. The primary `rtl-buddy` skill routes advanced work to focused test, dispatch, graph, formal, and implementation skills.

```bash
rb skill install
rb skill status
rb skill uninstall
```

| Scope | Claude Code | Codex |
| --- | --- | --- |
| User (default) | `~/.claude/skills/<member>/SKILL.md` | `~/.codex/skills/<member>/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/<member>/SKILL.md` | `<root>/.agents/skills/<member>/SKILL.md` |
| Explicit dir (`--dir PATH`) | `<PATH>/<member>/SKILL.md` | — |

`<member>` is `rtl-buddy`, `rtl-buddy-test`, `rtl-buddy-dispatch`, `rtl-buddy-graph`, `rtl-buddy-fpv`, `rtl-buddy-implementation`, or `rtl-buddy-pnr`.

- Use project scope only to override user-level skills in a project pinned to a different major version. The project root is found by walking up for `root_config.yaml`, then `.git/`.
- Use `--dir PATH` for a flat family outside the normal layout. It cannot be combined with `--project` or `--root`.
- Installing refreshes every member and removes obsolete skill directories at that scope. Install or uninstall once per scope you use. Re-run it after upgrading.
- Project installation updates `.gitignore`. Pass `--no-gitignore` to skip that.

## Local docs access

The wheel includes the docs for its installed version, so they work offline and match the running release:

```bash
rb docs list
rb docs show agents
rb docs show concepts/tests#interpret-results
rb --machine docs list
rb --machine docs show reference/yaml
```

`docs list` returns each page's slug, title, and frontmatter description. `docs show` takes a slug and an optional section anchor. In machine mode `docs list` uses the standard command envelope, while `docs show` prints the page payload as a bare JSON object.

Each published documentation version also has a static network mirror under `dev/` or `v<major>/`:

- `llms.txt` for discovery.
- `agent/catalog.json` for page and section metadata.
- `agent/pages/<slug>.md` for a raw page.
- `agent/sections/<slug>/<anchor>.md` for one section, with relative links rebased to version-pinned pages.

## Machine mode

Pass `--machine` before the subcommand:

```bash
rb --machine test basic
rb --machine regression -c regression.yaml
```

Supported commands print one JSON result to stdout and render plain stderr. File logs use JSON Lines. Python hook stdout becomes `hook.stdout` events; redirect child-process stdout to protect the result. See [Hook execution context](concepts/plugins.md#handle-hook-execution-context).

Stderr summaries omit `PASS`, `SKIP`, and `XFAIL` rows but tally every verdict. Stdout results and the log's `summary` event retain all rows and counts. Human mode shows all rows unless `--print-failures-only` is set.

Per-phase status lines require `--verbose` or `--debug`. Dispatch progress, job announcements, hooks, warnings, and errors remain visible.

## Know which commands write a log

Flow commands (`test`, `regression`, `synth`, `power`, and so on) write `<command_root>/rtl_buddy.log`, replacing the previous run's log.

Read commands (`rb phys`, `rb cov`, `rb graph query|path|explain`, `rb xplr`, and flow `--list`) preserve that log. They send events to stderr and JSON results to stdout. Debug the flow using the log that produced its artefacts.

## Parse command results

Structured commands print this top-level shape:

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

`meta.cwd` is the invocation directory and `meta.git` describes the project root, so they differ when `rb` runs from outside the checkout.

Parse the whole stdout with `json.loads()`. `command`, `exit_code`, `meta`, and the command-specific `payload` are stable. Optional fields may be added under `meta` or `payload`; an incompatible change needs a major version.

Payload conventions:

- Listing commands use `payload.names`.
- Regression results use `payload.results` and include `suite`.
- Elaboration results include top, source and diagnostic counts, elapsed time, peak memory, and `result_json`.
- `docs list` uses `payload.pages`.
- Coverage and formal results carry structured metrics and artefact paths.

See [Coverage](concepts/coverage.md) and [Formal Property Verification](concepts/fpv.md) for their payloads, and [Tests](concepts/tests.md#interpret-results) for statuses and exit codes.

## Read event logs

Each line of a machine-mode `rtl_buddy.log` is one JSON event:

```json
{"event":"sim.completed","test":"smoke","duration_sec":4.2,"message":"smoke: simulation completed in 4.20s"}
{"event":"postproc.completed","test":"smoke","result":"PASS","desc":"smoke completed","message":"smoke: post-processing completed with result PASS"}
```

Switch on `event` and read its fields; do not parse `message`. For a test, `postproc.completed.result` and `.desc` are the verdict. A multi-suite run also writes a log in each suite directory.
