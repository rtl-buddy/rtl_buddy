---
description: How to run rtl_buddy effectively from an AI agent, including local docs access, machine mode, log formats, and the recommended validation workflow.
---

# For Agents

Use this page to run `rtl_buddy` effectively from an AI agent, including local docs access, machine mode, log formats, and the recommended validation workflow.

## Local docs access

Use the bundled docs commands first when you need CLI or YAML reference:

```bash
rtl-buddy docs list
rtl-buddy docs show agents
rtl-buddy --machine docs show reference/yaml
```

`docs list` shows each page's slug, title, and summary. `docs show --machine` returns lightweight metadata plus the canonical Markdown for the selected page. GitHub Pages remains a convenient human-facing fallback.

## Agent Skill Install

The `rtl_buddy` wheel bundles an agent skill for Claude Code and Codex. Users run a one-time install to materialize it into their agent skill directories:

```bash
rtl-buddy skill install             # default: user-level
rtl-buddy skill install --project   # project-level (overrides user-level for that project)
rtl-buddy skill install --root PATH # explicit target (implies project-level layout)
rtl-buddy skill status              # report installed version vs current
rtl-buddy skill uninstall           # remove skill files
```

Targets:

| Scope | Claude Code | Codex |
|-------|-------------|-------|
| User (default) | `~/.claude/skills/rtl_buddy/SKILL.md` | `~/.codex/skills/rtl_buddy/SKILL.md` |
| Project (`--project`) | `<root>/.claude/skills/rtl_buddy/SKILL.md` | `<root>/.agents/skills/rtl_buddy/SKILL.md` |

User-level is the recommended default — one install per machine, one place to keep current. Project-level is an opt-in override for projects pinned to a divergent `rtl_buddy` major; Claude Code's resolution order puts the project copy first, so both can coexist.

For project-level installs, add the target dirs to your project's `.gitignore` — the install command prints the exact lines. `rtl-buddy skill install --project` discovers the project root via `root_config.yaml` (falling back to `.git/`), so it is safe to run from a `verif/` subdirectory.

## Always use machine mode

Run `rtl_buddy` with `--machine` in all agent-driven workflows:

```bash
rtl-buddy --machine test basic
rtl-buddy --machine regression -c design/regression.yaml
```

In machine mode:

- `rtl_buddy.log` is written as **JSON Lines** (one JSON object per line) instead of human-readable text.
- Console output switches to plain, colorless text — no Rich formatting, no spinners.
- All structured event fields (event name, status, durations, paths) are present in the log.

This makes it reliable to parse outcomes from `rtl_buddy.log` without screen-scraping.

## Log file locations

| File | Description |
|------|-------------|
| `rtl_buddy.log` | Orchestration log; JSONL in machine mode, human-readable otherwise |
| `artefacts/{test_name}/test.log` | Simulation stdout for each test |
| `artefacts/{test_name}/test.err` | Simulation stderr for each test |
| `artefacts/{test_name}/test.randseed` | Seed used for this test run |
| `artefacts/{test_name}/coverage.dat` | Coverage database (if coverage is enabled) |
| `artefacts/{test_name}/compile.log` | Compile transcript |
| `artefacts/{test_name}/run-NNNN/test.log` | Per-iteration output for `randtest` |
| `test.log` | Symlink to the most recent test's log |
| `test.err` | Symlink to the most recent test's stderr |
| `test.randseed` | Symlink to the most recent test's seed |

All files are written relative to the suite directory where `rtl_buddy` is invoked.

## Machine mode log format

Each line in `rtl_buddy.log` (machine mode) is a JSON object:

```json
{"event": "sim.completed", "test": "smoke", "duration_sec": 4.2, "message": "smoke: simulation completed in 4.20s"}
{"event": "postproc.completed", "test": "smoke", "result": "PASS", "desc": "smoke completed", "message": "smoke: post-processing completed with result PASS (smoke completed)"}
```

Key fields:

- `event`: dotted event name identifying what happened (e.g. `sim.start`, `compile.failed`, `postproc.completed`)
- `message`: the human-readable message corresponding to the event
- Other fields are event-specific (name, duration, seed, exit code, etc.)

## How pass/fail is detected

Agents authoring tests need to follow the parser that `rtl_buddy` actually uses:

- If `tests.yaml` sets `uvm:`, `rtl_buddy` parses the UVM Report Summary and compares it against `max_warns` / `max_errors`.
- Otherwise, `rtl_buddy` parses `artefacts/{test_name}/test.log` and expects one stdout line starting with `PASS` or `FAIL`.
- When emitting `FAIL`, also print an `ERR:` or `FAT:` line because the default failure parser expects it.
- If neither `PASS` nor `FAIL` appears, the test result becomes `NA`.
- Do not rely on simulator exit code alone for non-UVM pass/fail signalling.

Minimal non-UVM example:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

In machine mode, the authoritative per-test outcome appears in the `postproc.completed` event's `result` and `desc` fields.

## Recommended validation workflow

```bash
# 1. Check rtl_buddy version
rtl-buddy --version

# 2. Dry-run: verify pre-flight config without compiling or simulating
rtl-buddy --machine test basic --early-stop pre

# 3. Run a single test
rtl-buddy --machine test basic

# 4. Check the log for outcome
grep '"event"' rtl_buddy.log | tail -5

# 5. Run a full regression
rtl-buddy --machine regression -c design/regression.yaml
```

Use `--early-stop pre` to validate that config files, model paths, and testbench paths all resolve correctly before committing to a compile step.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All tests passed |
| 1 | One or more tests failed |
| 2 | Fatal configuration or environment error |

## Version checking

Always verify the installed version before running, especially in CI or after dependency updates:

```bash
rtl-buddy --version
```

The version follows semantic versioning. Breaking YAML schema or CLI changes are signaled by a major version bump.
