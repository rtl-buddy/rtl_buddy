# rtl_buddy — AI Agent Guide

## Role

This repo is the source-of-truth implementation of the `rtl_buddy` CLI.

## Canonical Guidelines

Read these before opening issues or PRs, or before changing runtime behavior:

- [docs/development/guidelines.md](docs/development/guidelines.md) — engineering rules: execution contexts, path ownership, artifact layout, subprocesses, dependencies, logging, errors, validation, releases, **quirks & known issues**, **issue triage**, and **milestones**.
- [docs/development/docs.md](docs/development/docs.md) — documentation authoring rules.
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — contributor entry point that links to both.

Issue conventions worth knowing up front:

- Type, Priority, and Effort are org-level GitHub Issue Fields, not labels. Templates under `.github/ISSUE_TEMPLATE/` pre-bind Type.
- Area is captured with `area/*` labels, kept consistent across all rtl-buddy repos: `area/test`, `area/wave`, `area/cdc`, `area/fpv`, `area/abv`, `area/mut`, `area/pd`, `area/hier`, `area/axi-profile`, `area/hub`, `area/skill`, `area/workflow`, `area/config`, `area/tooling`, `area/infra`. Plus `discussion`. The taxonomy is defined once in `.github/labels.json` and propagated with `.github/sync-labels.sh`; see the guidelines table for what each covers.
- The `version/{patch,minor,major}` labels are PR-only and drive the release workflow.
- Multi-issue long-running efforts get a theme-named milestone (e.g. "Hub Phase 3"), not a version-named one.

Where this file overlaps with the canonical guidelines, treat the guidelines as authoritative.

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
├── config/
│   ├── root.py            # discover_project_root(), RootConfig
│   ├── model.py           # ModelConfig (models.yaml)
│   ├── test.py            # TestConfig / TestConfigFile (tests.yaml)
│   ├── synth.py           # SynthConfig, SynthSuiteConfig, SynthRegConfig, SynthToolConfig (synth.yaml)
│   ├── spec.py            # SpecConfig / SpecBlock / SpecCoverageItem (specs.yaml)
│   └── ...                # platform, rtl, verible, coverage, coverview, reg
├── runner/test_runner.py  # PRE -> COMP -> SIM -> POST execution
├── runner/synth_runner.py # synthesis dispatch; resolves tool config and invokes backend
├── runner/synth_results.py # SynthResults / SynthPassResults / SynthFailResults / SynthSkipResults
└── tools/
    ├── synth_yosys.py     # Yosys backend: filelist → synth.ys script → yosys invocation
    ├── cdc_rtl_buddy.py   # rtl-buddy-cdc subprocess wrapper, parses JSON report
    ├── hier_rtl_buddy_view.py # rtl-buddy-view subprocess wrapper for `rb hier`
    ├── spec_trace.py      # discover_spec_configs, build_coverage_map, etc.
    └── ...                # filelist, sim, postproc, verible wrappers
```

## Implementation Notes

- `rtl_buddy.py` owns CLI wiring, global options, and command dispatch.
- `RootConfig` selects platform, builder, verible, and regression config from `root_config.yaml`.
- `TestRunner` drives PRE, COMPILE, SIM, and POST with early-stop support.
- `VlogSim` captures the suite cwd once, but both compile and sim now run from per-test workspaces under `artefacts/<sanitized-test>/`; repeated runs use `artefacts/<sanitized-test>/run-0001/`, while `test.log`, `test.err`, and `test.randseed` in the suite directory remain latest-run symlinks.
- `VlogFilelist` handles `.f` parsing and transformations. It resolves model entries from the real `models.yaml` location, resolves testbench entries from the suite cwd, and writes paths relative to the directory containing the generated `run.f`.
- Nested raw coverage paths such as `artefacts/<test>/run-0001/coverage.dat` must preserve the suite-root hint during LCOV/Coverview `SF:` rewriting. When updating coverage path logic, make sure duplicate basenames still resolve against the originating suite root instead of falling back to repo-wide basename matching.
- Hook scripts (`sweep`, `preproc`, `postproc`) are executed dynamically and should be treated as compatibility-sensitive APIs.
- `SynthRunner` resolves a `SynthToolConfig` from `root_cfg.get_synth_tool_cfg(tool_name)`, merges any `tool_overrides` from the `SynthConfig`, then dispatches to `YosysSynth`. Opts resolution: root-config `opts` are the baseline; per-run `tool_overrides.<tool>` keys overwrite matching fields.
- `YosysSynth` writes `synth.f` via `VlogFilelist` (with `unroll=True, strip=True, deduplicate=True`), then generates `synth.ys`. Source files are emitted as individual `read_verilog -sv -defer` commands (not `-f filelist`) so Yosys only elaborates the top hierarchy. Pass/fail is determined by exit code then `ERROR:` line scan.
- `rb hier <model>` (`tools/hier_rtl_buddy_view.py`) writes a stripped+deduplicated filelist to `artefacts/hier/<model>/hier.f`, then shells out to `rtl-buddy-view` with `--top <model> --filelist hier.f --format <fmt>` plus optional `--output`, `--frontend`, `--cdc-annotations`, `--clock-legend`. The renderer's stdout passes through to the terminal when `-o` is not given (so `rb hier x --format dot | dot -Tsvg ...` works); stderr is captured to `hier.log`. The integration is at subprocess granularity — rtl_buddy is not coupled to the viewer's Python API. The viewer's JSON contract (`schema_version`, `tool.*`, `design.top`, `nodes`, `edges`) is guarded by `test_json_contract_keys_present_and_typed` in rtl-buddy-view.

## Validation

For validation policy (what to run for which change), see [Validation](docs/development/guidelines.md#validation) in the engineering guidelines. Concrete commands:

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

## Code Quality

This repo uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. CI enforces both on every PR via `.github/workflows/lint.yml`.

Install the pre-commit hook once after cloning so Ruff runs automatically on every commit:

```bash
uv tool install pre-commit
pre-commit install
```

To run Ruff manually:

```bash
uv run ruff check          # lint
uv run ruff format         # format in place
uv run ruff format --check # check only (what CI does)
```

To update the pre-commit hook version:

```bash
pre-commit autoupdate
```

## Testing

The `pytest` suite under `tests/` is run in CI by `.github/workflows/test.yml` with coverage on every push and PR. Locally:

```bash
uv run pytest                                    # run the suite
uv run pytest --cov                              # with coverage summary
uv run pytest --cov --cov-report=term-missing    # show uncovered lines
uv run pytest --cov --cov-report=html            # write htmlcov/index.html
```

Coverage configuration lives in `[tool.coverage.*]` in `pyproject.toml` (source = `src/rtl_buddy`, excludes the bundled `skill/` and `docs/`). No `--cov` is set in `pytest.ini` so plain `pytest` stays fast; pass `--cov` explicitly when you want a coverage run.

## Logging and Error Handling

Policy lives in [Logging](docs/development/guidelines.md#logging) and [Error Handling](docs/development/guidelines.md#error-handling). Code-level helpers and entry points in this repo:

- `log_event(logger, level, "event.name", **fields)` in `src/rtl_buddy/logging_utils.py` — the only sanctioned runtime-logging call. Do not use `logger.info(f"...")` directly.
- `_human_message()` in the same module — add a `case` entry for any new WARNING or ERROR event so the human-mode message stays clear.
- Exception classes: `FatalRtlBuddyError` (top-level `run()` exits with code 2), `FilelistError` (caught by `TestRunner`, becomes `FilelistFailResults`), and the setup-failure string contract from `pre()` / `_expand_tests_with_sweep()` (becomes `SetupFailResults`).
- Do not use `logger.critical()` — the old `ExitHandler` abort pattern has been removed.
- Console helpers: `emit_console_text()` for direct user-facing output, `render_summary()` for result tables (Rich on console, plain text in the log), `task_status()` for spinners on long-running phases.

## Skill Distribution

The rtl_buddy agent skill ships inside this wheel at `src/rtl_buddy/skill/` and is materialized by `rtl-buddy skill install`. There is no separate skill repo — the legacy `rtl-buddy-codex-skill` repo is deprecated. Dev-only audit skills live under `.claude/skills/` in this repo and are not distributed.

### Rules when editing skill content

- `src/rtl_buddy/skill/SKILL.md` is the single source consumed by both Claude Code (at `.claude/skills/rtl_buddy/`) and Codex (at `.agents/skills/rtl_buddy/` for project scope, `~/.codex/skills/rtl_buddy/` for user scope).
- Keep `SKILL.md` ≤60 lines and agent-specific. Anything covered by the docs site should cite <https://rtl-buddy.github.io/rtl_buddy/>, not restate it.
- Agent-facing local docs access goes through `rtl-buddy docs ...`. The wheel ships `docs/**/*.md` directly (via a symlink at `src/rtl_buddy/docs`) so docs are always in sync with the installed version.
- Any edit to `SKILL.md` takes effect for users only after they re-run `rtl-buddy skill install`. `rtl-buddy skill status` surfaces stale installs via the `.rtl_buddy_skill_version` marker.
- `src/rtl_buddy/skill/gitignore_snippet.txt` is printed by project-level installs and by `rtl-buddy skill print-gitignore`.
- Files in `src/rtl_buddy/skill/` are included in the wheel automatically via hatchling's `packages = ["src/rtl_buddy"]`. Adding new files to the skill dir requires no extra config. The `docs/` directory ships via `force-include` in `pyproject.toml` and is excluded from package discovery to avoid double-inclusion.

### Install scope policy

- **Default is user-level** (`~/.claude/skills/rtl_buddy/`, `~/.codex/skills/rtl_buddy/`). This is deliberate: the skill is workflow-pattern guidance that changes rarely across rtl_buddy versions, and a single copy per machine nudges users to keep rtl_buddy versions aligned across projects.
- `--project` (or `--root PATH`) opts into project-level install at `<root>/.claude/skills/rtl_buddy/` and `<root>/.agents/skills/rtl_buddy/`. Claude Code's project-level precedence means a project-level copy overrides the user-level one when both exist — this is the escape hatch for projects that pin a divergent rtl_buddy major.
- Do not flip the default to project-level without rediscussion; the precedence model makes user-level-plus-project-override the clean path.

### Project root discovery

`config.root.discover_project_root()` is the single shared entry point for locating the project root. It walks up from `cwd` for `root_config.yaml`, then for `.git/`. Pass `fallback_cwd=True` to return `cwd` silently when neither is found (used by the spec commands); the default raises `FatalRtlBuddyError`. This handles agents invoking from `verif/<suite>/` subdirs — `Path.cwd()` alone would be wrong.

## Release Workflow

Release policy (when stable vs pre-release, do-not-do rules) lives in [Releases](docs/development/guidelines.md#releases). This section covers what the workflow does mechanically.

### Stable release

Triggered by merging a PR to `main` with a `version/{patch,minor,major}` label. On merge:

1. The workflow computes the next `vMAJOR.MINOR.PATCH` tag, creates it, and pushes it.
2. A GitHub release is created (not marked pre-release).
3. The wheel is built (hatch-vcs derives the version from the tag) and published to PyPI.
4. Docs are deployed to `gh-pages` under the matching `v{major}` alias; `latest` is updated if this is the highest major.

### Pre-release

Cut from a feature branch via `workflow_dispatch` with the **Mark as pre-release** checkbox enabled:

1. The workflow appends `rcN` to the computed base tag (PEP 440). If `v2.3.0rc1` already exists, the next is `v2.3.0rc2`.
2. A GitHub release is created and marked **pre-release**.
3. The wheel is published to PyPI as a pre-release version (e.g. `2.3.0rc1`). Unqualified version ranges (`>=2.2.0`) will not resolve to it.
4. Docs are **not** published — the `latest` alias is not updated.

The version is computed from the latest stable tag at dispatch time. If `main` advances and releases the same bump tier before your branch merges, the next RC will shift to the following version — that is expected and acceptable.

### Infrastructure notes

- GitHub Pages must be configured to publish from the `gh-pages` branch.
- A `GH_PAGES_TOKEN` secret is required because pushes made with the default `GITHUB_TOKEN` do not reliably trigger downstream docs publishing from automation-created tags.
- Update and tag any downstream integrations that track this repo after a stable release.
