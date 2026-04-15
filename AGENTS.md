# rtl_buddy — AI Agent Guide

## Role

This repo is the source-of-truth implementation of the `rtl_buddy` CLI.

Do implementation work here first.

## Key Files

```text
src/rtl_buddy/
├── __main__.py            # package entry point
├── rtl_buddy.py           # Typer CLI and top-level command flow
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
2. Update any downstream agent docs if command behavior, YAML schema, version expectations, or validation notes changed.
3. Update downstream integrations to the intended commit as needed.

## Release Workflow

1. Merge to `main` in this repo and tag (e.g. `v2.0.0`).
2. Update and tag any downstream integrations that track this repo.
