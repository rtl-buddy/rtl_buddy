---
description: How rtl_buddy is built for AI-agent use — the bundled skill, local docs access, machine mode, and the structured log and stdout contracts agents can rely on.
---

# Agent use of rtl-buddy

`rtl_buddy` is designed to be driven by AI coding agents as well as humans. This page describes the affordances that make agent integration practical:

- a **bundled agent skill** that installs into Claude Code and Codex,
- **local docs access** through the `rtl-buddy docs` command, so reference material always matches the installed version,
- a **machine mode** (`--machine`) that switches the orchestration log to JSON Lines and emits a stable stdout envelope on exit,
- and deterministic [exit codes](concepts/tests.md#exit-codes) that let an orchestrator distinguish test failures from configuration errors.

## Bundled agent skill

The `rtl_buddy` wheel ships an agent skill that teaches Claude Code and Codex the conventions for invoking `rtl_buddy` — when to use `--machine`, where logs are written, how multi-suite runs lay out artefacts, and which docs to consult. Because the skill ships with the wheel, its content is locked to the installed `rtl_buddy` major version.

Users materialize the skill once with `rtl-buddy skill install`:

```bash
rtl-buddy skill install             # default: user-level
rtl-buddy skill install --project   # project-level (overrides user-level for that project)
rtl-buddy skill uninstall           # remove skill files
```

See [cli reference](reference/cli.md) for full `rb skill` interface.

Install targets:

| Scope | Claude Code | Codex |
|-------|-------------|-------|
| User (default) | `~/.claude/skills/rtl_buddy/SKILL.md` | `~/.codex/skills/rtl_buddy/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/rtl_buddy/SKILL.md` | `<root>/.agents/skills/rtl_buddy/SKILL.md` |

User-level is the default because the skill is workflow-pattern guidance that changes rarely across `rtl_buddy` versions; a single copy per machine encourages keeping `rtl_buddy` aligned across projects. Project-level installs are an opt-in override for projects pinned to a divergent `rtl_buddy` major — Claude Code's resolution order puts the project copy first, so both scopes can coexist.

For project-level installs, the install command prints the `.gitignore` lines to add. Project root is discovered by walking up for `root_config.yaml` (falling back to `.git/`), so `rtl-buddy skill install --project` is safe to run from a `verif/` subdirectory.

## Local docs access

The wheel ships the full Markdown docs site alongside the CLI, exposed through:

```bash
rtl-buddy docs list
rtl-buddy docs show agents
rtl-buddy --machine docs show reference/yaml
```

This is the recommended reference surface for agents: the docs are local (no network), and their content always matches the installed version of `rtl_buddy`. `docs list` enumerates each page's slug, title, and description; `docs show` returns the canonical Markdown for a single page. GitHub Pages at <https://rtl-buddy.github.io/rtl_buddy/> remains available as a human-facing fallback.

## Machine mode

Passing `--machine` switches `rtl_buddy` into a mode designed for programmatic consumption:

- `rtl_buddy.log` is written as **JSON Lines** instead of human-readable text.
- Console output drops Rich formatting, colors, and spinners.
- Commands that produce structured results print a single JSON envelope to **stdout** on exit.

The intent is that an orchestrator can determine the outcome of a run by parsing the stdout envelope, and reconstruct timing or per-event detail from `rtl_buddy.log`, without screen-scraping human-formatted output.

```bash
rtl-buddy --machine test basic
rtl-buddy --machine regression -c design/regression.yaml
```

### Stdout envelope

In machine mode, structured-result commands print a single JSON object to stdout on exit:

```json
{
  "command": "test",
  "exit_code": 0,
  "meta": {
    "rtl_buddy_version": "2.4.0",
    "argv": ["rtl-buddy", "--machine", "test", "basic"],
    "cwd": "/path/to/suite",
    "git": {"branch": "main", "commit": "abc1234", "modified": 0, "staged": 0}
  },
  "payload": {
    "results": [
      {"name": "basic", "result": "PASS", "desc": "basic completed"}
    ]
  }
}
```

The envelope shape is the same across commands:

- `command` — the subcommand that was run (`"test"`, `"regression"`, `"synth"`, …).
- `exit_code` — integer exit code (see [exit codes](concepts/tests.md#exit-codes)).
- `meta` — version, argv, working directory, and git status at invocation.
- `payload` — command-specific structured data.

The top-level envelope fields are reserved and versioned by `meta.rtl_buddy_version` under `rtl_buddy`'s normal semantic-versioning rules. Adding optional fields under `meta` or `payload` is non-breaking; removing fields, renaming fields, changing field types, or changing the meaning of an existing field is breaking.

Conventions inside `payload`:

- `--list` commands (`test --list`, `synth --list`, …) populate `payload.names`.
- Regression commands populate `payload.results`, with a `"suite"` field on each entry.
- `docs list` populates `payload.pages` with `slug`, `title`, and `description` from page frontmatter.

### JSONL log format

In machine mode, each line of `rtl_buddy.log` is a JSON object describing one event:

```json
{"event": "sim.completed", "test": "smoke", "duration_sec": 4.2, "message": "smoke: simulation completed in 4.20s"}
{"event": "postproc.completed", "test": "smoke", "result": "PASS", "desc": "smoke completed", "message": "smoke: post-processing completed with result PASS (smoke completed)"}
```

Common fields:

- `event` — dotted event name identifying what happened (`sim.start`, `compile.failed`, `postproc.completed`, …).
- `message` — the human-readable rendering of the event.
- Event-specific fields — test name, duration, seed, exit code, file paths, etc.

The authoritative per-test outcome is the `postproc.completed` event's `result` and `desc` fields. For multi-suite commands, each suite directory gets its own `rtl_buddy.log`.
