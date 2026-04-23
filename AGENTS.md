# rtl_buddy — AI Agent Guide

## Role

This repo is the source-of-truth implementation of the `rtl_buddy` CLI.

## Key Files

```text
src/rtl_buddy/
├── __main__.py            # package entry point
├── rtl_buddy.py           # Typer CLI and top-level command flow
├── skill_install.py       # `rtl-buddy skill ...` subcommands
├── skill/                 # bundled agent skill (shipped in the wheel)
├── logging_utils.py       # log_event(), setup_logging(), console helpers
├── errors.py              # FatalRtlBuddyError, FilelistError, SetupScriptError
├── seed_mode.py           # seed handling enum
├── config/                # YAML-backed config classes
├── runner/test_runner.py  # PRE -> COMP -> SIM -> POST execution
└── tools/                 # filelist, sim, postproc, verible wrappers
```

## Development Rules

- Keep changes targeted. Avoid broad refactors unless the task requires them.
- Preserve CLI behavior unless intentionally changing it.
- Treat YAML config classes and runtime behavior as part of the public interface for downstream RTL projects.
- When adding or changing behavior, update downstream docs and validation assets after validating the implementation.

## Implementation Notes

- `rtl_buddy.py` owns CLI wiring, global options, and command dispatch.
- `RootConfig` selects platform, builder, verible, and regression config from `root_config.yaml`.
- `TestRunner` drives PRE, COMPILE, SIM, and POST with early-stop support.
- `VlogSim` handles compile/sim command construction, log paths, seeds, and timeout behavior.
- `VlogFilelist` handles `.f` parsing and transformations.
- Hook scripts (`sweep`, `preproc`, `postproc`) are executed dynamically and should be treated as compatibility-sensitive APIs.

## Validation

Typical checks:

```bash
# from a project root that has `rtl_buddy` installed
./venv/bin/python -m rtl_buddy regression -c regression.yaml
./venv/bin/python -m rtl_buddy filelist test_module -c design/example_block/src/models.yaml
./venv/bin/python -m rtl_buddy verible syntax design/example_block/src/test_module.sv
./venv/bin/python -m rtl_buddy --machine docs list
./venv/bin/python -m rtl_buddy --machine docs show agents

# from a suite directory
cd design/example_block/verif
../../../venv/bin/python -m rtl_buddy test basic
```

If validating the dev checkout directly, install this repo into the target venv and confirm with `./venv/bin/python -m rtl_buddy --version`.

## Logging Practices

All runtime logging goes through `log_event()` in `src/rtl_buddy/logging_utils.py`. Do not use `logger.info(f"...")` directly — use `log_event(logger, level, "event.name", key=value, ...)` so that both human and machine modes produce correct output.

### How it works

- **Human mode (default)**: `_human_message()` converts each event into a readable sentence for `rtl_buddy.log` and the console. Machine-oriented fields are not visible.
- **Machine mode (`--machine`)**: `rtl_buddy.log` is written as JSON Lines with the event name, all fields, and the human message. Console output is plain text.

### Adding new events

1. Choose a dotted event name following the existing convention (e.g. `compile.start`, `sim.timeout`, `suite_config.load_failed`).
2. Call `log_event(logger, logging.<LEVEL>, "your.event", field1=val1, ...)`.
3. If the event is logged at **WARNING or above**, add a dedicated `case` entry in `_human_message()`. Users see these messages directly, so they must be clear and actionable.
4. DEBUG and INFO events may rely on the wildcard fallback formatter, which converts `"foo.bar"` to `"foo bar"` and appends select fields.

### Error handling

- Fatal config/environment errors: log at `logging.ERROR`, then `raise FatalRtlBuddyError(...)`. The top-level `run()` catches these and exits with code 2.
- Per-test filelist failures: log at `logging.ERROR`, then `raise FilelistError(...)`. `TestRunner` catches these and records a `FilelistFailResults`.
- Sweep/preproc script failures: return an error string from `pre()` or `_expand_tests_with_sweep()`. The caller records a `SetupFailResults` and continues.
- Do not use `logger.critical()` — the old `ExitHandler` abort pattern has been removed.

### Console output

- Use `emit_console_text()` for direct user-facing output (e.g. git status banner, regression directory).
- Use `render_summary()` for result tables — it writes a Rich table to the console and plain text to the log file.
- Use `task_status()` for spinners on long-running phases (compile, sim). Falls back to plain text on non-interactive terminals.

## Required Follow-Through

After meaningful `rtl_buddy` changes:

1. **If any CLI command, flag, or help text changed**: run `python scripts/gen_cli_reference.py` and commit the updated `docs/reference/cli.md` in the same PR. The file is committed to the repo and must stay in sync — CI will catch drift via `python scripts/gen_cli_reference.py --check`.
2. **If docs content or docs CLI behavior changed**: run `python scripts/gen_docs_resource.py` and commit the updated `src/rtl_buddy/docs_data.json` in the same PR. CI also checks drift via `python scripts/gen_docs_resource.py --check`.
3. Update any downstream agent docs if command behavior, YAML schema, version expectations, or validation notes changed.
4. Update downstream integrations to the intended commit as needed.

## Skill Distribution

The rtl_buddy agent skill ships inside this wheel at `src/rtl_buddy/skill/` and is materialized by `rtl-buddy skill install`. There is no separate skill repo — the legacy `rtl-buddy-codex-skill` repo is deprecated.

### Rules when editing skill content

- `src/rtl_buddy/skill/SKILL.md` is the single source consumed by both Claude Code (at `.claude/skills/rtl_buddy/`) and Codex (at `.agents/skills/rtl_buddy/` for project scope, `~/.codex/skills/rtl_buddy/` for user scope).
- Keep `SKILL.md` ≤60 lines and agent-specific. Anything covered by the docs site should cite <https://rtl-buddy.github.io/rtl_buddy/>, not restate it.
- Agent-facing local docs access now goes through `rtl-buddy docs ...`, backed by packaged metadata in `src/rtl_buddy/docs_data.json`.
- Any edit to `SKILL.md` takes effect for users only after they re-run `rtl-buddy skill install`. `rtl-buddy skill status` surfaces stale installs via the `.rtl_buddy_skill_version` marker.
- `src/rtl_buddy/skill/gitignore_snippet.txt` is printed by project-level installs and by `rtl-buddy skill print-gitignore`.
- Package-data for the skill dir is declared in `pyproject.toml` under `[tool.setuptools.package-data]`. Adding new files to `src/rtl_buddy/skill/` requires updating that glob.

### Install scope policy

- **Default is user-level** (`~/.claude/skills/rtl_buddy/`, `~/.codex/skills/rtl_buddy/`). This is deliberate: the skill is workflow-pattern guidance that changes rarely across rtl_buddy versions, and a single copy per machine nudges users to keep rtl_buddy versions aligned across projects.
- `--project` (or `--root PATH`) opts into project-level install at `<root>/.claude/skills/rtl_buddy/` and `<root>/.agents/skills/rtl_buddy/`. Claude Code's project-level precedence means a project-level copy overrides the user-level one when both exist — this is the escape hatch for projects that pin a divergent rtl_buddy major.
- Do not flip the default to project-level without rediscussion; the precedence model makes user-level-plus-project-override the clean path.

### Project root discovery

`skill_install._discover_project_root()` reuses `config.root._discover_root_cfg()` (walks up for `root_config.yaml`), then falls back to walking up for `.git/`, then errors. This handles agents invoking from `verif/<suite>/` subdirs — `Path.cwd()` would be wrong.

## Release Workflow

1. Merge to `main` in this repo and tag (e.g. `v2.0.0`).
2. The docs workflow publishes versioned MkDocs output to the `gh-pages` branch with `mike`: `main` updates the `dev` docs, release tags update the matching `v<major>` docs line, and the newest released major also moves the `latest` alias.
3. GitHub Pages must be configured to publish from the `gh-pages` branch, and the repo must provide a `GH_PAGES_TOKEN` secret because pushes made with the default `GITHUB_TOKEN` do not trigger branch-based Pages publishing or downstream workflows from release-created tags.
4. Update and tag any downstream integrations that track this repo.
