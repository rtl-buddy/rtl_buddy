# For Agents

This page covers how AI agents should interact with `rtl_buddy`. It describes the bundled skill, machine mode, log formats, and the recommended validation workflow.

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
| `logs/{test_name}.log` | Simulation stdout for each test |
| `logs/{test_name}.err` | Simulation stderr for each test |
| `logs/{test_name}.randseed` | Seed used for this test run |
| `test.log` | Symlink to the most recent test's log |
| `test.err` | Symlink to the most recent test's stderr |
| `test.randseed` | Symlink to the most recent test's seed |

All files are written relative to the directory where `rtl_buddy` is invoked.

## Machine mode log format

Each line in `rtl_buddy.log` (machine mode) is a JSON object:

```json
{"event": "test.pass", "name": "smoke", "duration": 4.2, "seed": 31310, "msg": "smoke passed"}
{"event": "suite.summary", "passed": 3, "failed": 0, "total": 3, "msg": "3/3 passed"}
```

Key fields:

- `event`: dotted event name identifying what happened (e.g. `test.pass`, `test.fail`, `compile.error`)
- `msg`: the human-readable message corresponding to the event
- Other fields are event-specific (name, duration, seed, exit code, etc.)

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
