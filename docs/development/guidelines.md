---
description: Engineering contracts for rtl_buddy paths, artifacts, subprocesses, dependencies, logging, validation, and releases.
---

# Engineering Guidelines

Preserve these contracts in runtime and documentation changes. Treat an implementation mismatch as a bug or a documented exception.

## Public Contracts

CLI behavior, YAML loading, artifact layout, machine output, event names, and bundled-skill behavior are public interfaces. For intentional changes, update tests, docs, generated references, and downstream validation assets in the same PR.

## Quirks and Known Issues

Record surprising defaults, limitations, and workarounds in [Quirks & Known Issues](../known-issues.md). Give each quirk one H2 section that states the effect and the action to take.

## Execution Contexts

Use explicit contexts, never ambient `os.getcwd()`:

- `invocation_cwd`: the directory where the user ran `rb`. Use it to resolve relative CLI arguments before they become absolute.
- `command_root`: the directory containing the command's primary config file.
- `suite_dir`: the command root for per-suite flows such as `tests.yaml`, `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, and `fpga.yaml`.
- `artifact_dir`: the generated workspace for one command item, normally `suite_dir/artefacts/<name>`.

Config-driven commands use their primary config's directory as `command_root`. Managed outputs go below it, external tools run from their artifact directory, and explicit CLI paths resolve from `invocation_cwd`.

## Command Roots

Use these roots unless a command documents a narrower exception:

| Command | Command root | Artifact root | External tool CWD |
|---|---:|---:|---:|
| `test` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | compile: `<artifact>`; sim: `<artifact>` |
| `randtest` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | compile: `<artifact>`; sim: `<artifact>/run-NNNN` |
| `regression` | `dirname(regression.yaml)` | each suite's `<suite>/artefacts/<test>` | same as `test` per suite |
| `wave --resim` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | same as `test` |
| `synth` | `dirname(synth.yaml)` | `<suite>/artefacts/<synth>` | `<artifact>` |
| `fpv` | `dirname(fpv.yaml)` | `<suite>/artefacts/<fpv>` | `<artifact>` |
| `pnr` | `dirname(pnr.yaml)` | `<suite>/artefacts/<pnr>` | `<artifact>` |
| `power` | `dirname(power.yaml)` | `<suite>/artefacts/<power>` | `<artifact>` |
| `fpga` | `dirname(fpga.yaml)` | `<suite>/artefacts/<fpga>` | `<artifact>` |
| `fpga-regression` | `dirname(fpga.yaml)` | `<suite>/artefacts/<fpga>` | `<artifact>` |
| `hier --view dut` | `dirname(models.yaml)` | `<model_root>/artefacts/hier/<model>` | `<artifact>` |
| `hier --view tb` | `dirname(tests.yaml)` | `<suite>/artefacts/hier/<test-or-model>` | `<artifact>` |
| `axi-profile run` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile notebook` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile discover` | `dirname(models.yaml)` | `<model_root>/artefacts/axi/<model>` | `<artifact>` |
| `axi-profile gen-monitor` | `dirname(models.yaml)` | configured or explicit output; fallback artifact dir | `<artifact>` |
| `graph build` | project root | `<root>/artefacts/graph/` | viewer: `<model_root>/artefacts/hier/<model>` |
| `graph results` / query commands / `mcp` | project root | `<root>/artefacts/graph/` | none, except viewer-backed MCP tools |
| `filelist` | `dirname(models.yaml)` for config reads | explicit output path | no hidden tool CWD |
| `saif` | invocation CWD for explicit paths | explicit output path | no hidden tool CWD |
| `hub` | project root | `.rtl-buddy/...` | project root or `.rtl-buddy`, depending subcommand |
| `docs`, `skill` | no project execution context | none | none |

## Path Ownership

Resolve config-owned paths from the config file that owns them:

- `root_config.yaml` is discovered from the command root for config-driven commands.
- `regression.yaml` resolves listed suite configs relative to itself.
- `tests.yaml` resolves testbench filelists, hook script paths, and suite-local runtime assets relative to the suite directory.
- `models.yaml` resolves model filelist entries relative to the `models.yaml` file that defined them.
- `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml`, and `fpga.yaml` resolve their own fields relative to their config directory.

Pass absolute paths to external tools unless a value is intentionally artifact-relative.

## Artifact Layout

Write generated outputs under `artefacts/<name>/`. Keep compile outputs (`run.f`, `compile.log`, builder output) in the test root and randomized simulation output in `run-NNNN/`. Latest-run symlinks are conveniences, not durable storage.

Every run writes `result.json` beside its durable output. Consumers use this envelope, not log parsing, for verdicts. Envelope writes are best-effort and must not turn a passing run into a failure. Dispatch also collects copies under `<test>/dispatch/result-<tag>.json`.

## Subprocesses

Pass an explicit `cwd` to every external tool. Use the artifact directory unless the command documents another location. Use `run_managed_process()` for long-running tools; reserve `subprocess.run()` for short probes and helpers.

## Dependencies

Classify dependencies as required, integrated, pluggable, or pluggable curated; see [Installation](../install.md#dependency-types). Keep required Python dependencies minimal and use optional tools for feature-specific functionality.

For every external tool, update `docs/install.md`, `src/rtl_buddy/tool_manifest.py`, and `tests/test_tool_manifest.py` together. Keep `used_by`, optional status, minimum version, detector, install hint, and notes aligned. Document:

- the command or feature that needs it;
- whether it is integrated, pluggable, or pluggable curated;
- any required version or fork;
- optional sub-dependencies such as coverage, rendering, or notebook extras;
- the concept page that explains build or setup details.
- the `rb tool-check --explain <tool>` recovery hint.

Update [Tool Dependency Check](../concepts/tool-check.md) when manifest behavior changes.

## Logging

Send runtime events through `log_event()` in `logging_utils.py`; do not call `logger.info()` directly. Human mode renders readable text and machine mode writes JSON Lines.

When adding events:

- Use dotted names such as `compile.start`, `sim.timeout`, or `suite_config.load_failed`.
- Include structured fields that are stable and useful for agents.
- Add a dedicated human-message case for WARNING or ERROR events.
- Use `log_console_event()` only for default-verbosity liveness signals or output previously visible on stdout, such as captured hook `print()` calls.
- Keep DEBUG and INFO events concise enough for machine logs.

## Error Handling

Fatal config and environment errors should log at ERROR and raise `FatalRtlBuddyError`.
The top-level command exits with code 2.

Convert recoverable per-item failures into structured results. Use `FilelistError` for filelist failures caught by `TestRunner`, and return setup-failure strings from sweep or preproc failures so the suite records `SetupFailResults`.

## Validation And Follow-Through

Let validation scale with risk:

- Docs-only edits: run frontmatter and MkDocs strict checks.
- CLI help changes: regenerate `docs/reference/cli.md` and run the generated-reference check.
- Path, artifact, or subprocess changes: add focused tests proving roots, generated paths, and subprocess `cwd`.
- Shared command-dispatch or config-loader changes: run the affected test module subset, then broaden if the change crosses command families.

Report skipped checks and the reason. Complete the applicable follow-through:

1. If CLI command names, flags, help text, or output behavior changed, regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py`.
2. If a feature, command, optional extra, or external tool dependency changed, update `docs/install.md`.
3. If an external tool dependency changed, update `src/rtl_buddy/tool_manifest.py`, `tests/test_tool_manifest.py`, and `docs/concepts/tool-check.md` when tool-check behavior or coverage changes.
4. If docs changed, keep frontmatter valid and run the docs build. See [Documentation Guidelines](docs.md).
5. If behavior, YAML schema, version expectations, or validation workflows changed, update user docs and the bundled skill if agents rely on the behavior.
6. If release or packaging behavior changed, verify wheel inclusion rules and update downstream integrations after release.
7. Record new non-conventional behavior in `docs/known-issues.md`.
8. For `version/major`, add a complete `## vN to vM` section to `docs/migrations.md`.

## Bundled Skills

The bundled skill family ships from `src/rtl_buddy/skill/`; there is no separate source repository. Keep the primary skill focused on feature routing and each specialist on non-obvious topic guidance. Every member must stay below 8 KiB and link to local docs instead of copying reference material.

Project installs are overrides; user scope remains the default.

## Issue Triage

Set these GitHub fields on every issue:

- **Type** — the org-level Issue Type: `Bug`, `Feature`, or `Docs`. Set once on every issue.
- **Priority** — the org-level Issue Field: `Urgent`, `High`, `Medium`, or `Low`. Reflects how soon the work should land, not how big it is.
- **Effort** — the org-level Issue Field: `High`, `Medium`, or `Low`. Optional; fill it in when the answer is non-obvious.

Type, Priority, and Effort are fields, not labels. Apply one preferred `area/*` label when possible. Edit the shared taxonomy in `.github/labels.json` and run `.github/sync-labels.sh`; do not create labels by hand.

| Label | Covers |
|---|---|
| `area/test` | `test`, `randtest`, `regression`, and the compile/sim runner pipeline |
| `area/wave` | waveform viewing and integration (surfer, WCP) |
| `area/fpv` | formal property verification (`rb fpv`, sby plus commercial backends) |
| `area/abv` | assertion-based verification (SVA, properties) in sim |
| `area/mut` | mutation testing (`rb mut`) |
| `area/pd` | ASIC physical design: `synth`, `pnr`, `power` |
| `area/fpga` | FPGA implementation flow (`rb fpga`, Vivado + open backends) and FPGA-specific checks |
| `area/hier` | `hier` viewer and `rtl-buddy-view` integration |
| `area/axi-profile` | `axi-profile` discover, run, notebook, and monitor generation |
| `area/hub` | the hub server, marimo integration, hub event plumbing |
| `area/skill` | the bundled agent skill and `skill install` |
| `area/workflow` | spec-driven / end-to-end workflow orchestration |
| `area/config` | `root_config.yaml`, suite YAML loading, `filelist`, and model resolution |
| `area/tooling` | `tool-check`, `tool_manifest.py`, and external-tool integration |
| `area/infra` | CI workflows, packaging, release mechanics, dependencies, machine-mode logging, and the rtl-buddy CLI |

Use `discussion` for scope or design conversations. Reserve `version/*` labels for PRs.

## Pull Requests

Put `Closes #NN` in the PR description, one line per issue. Titles and ranges do not autoclose issues.

## Milestones

Use theme-named milestones for multi-issue efforts, not single issues or releases. Open one after the first issues are scoped; close it when the work finishes and move remaining work to a follow-up milestone.

## Releases

Merge stable releases to `main` with one `version/patch`, `version/minor`, or `version/major` label. Cut prereleases from feature branches by workflow dispatch. Do not merge a prerelease branch to release it or push a downstream pin before the PyPI release exists.

A `version/major` PR must add one `## vN to vM` section to `docs/migrations.md` covering every moved output, changed default, removed or renamed field, and downstream contract change.
