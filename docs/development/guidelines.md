---
description: Engineering guidelines for rtl_buddy maintainers, covering execution contexts, paths, artifacts, subprocesses, logging, errors, validation, and release-sensitive files.
---

# Engineering Guidelines

These are maintainer rules for code and docs changes in `rtl_buddy`.
They describe the contracts new work should preserve and existing code should converge toward.
When implementation and guideline disagree, treat the mismatch as a bug or documented exception.

## Public Contracts

Treat CLI behavior, YAML config loading, generated artifact layout, machine-mode output, event names, and bundled skill behavior as public interfaces.
Downstream RTL projects and automation depend on them.

Prefer targeted changes over broad refactors.
When a change intentionally alters a contract, update docs, tests, generated references, and downstream validation assets in the same PR.

## Quirks and Known Issues

When behavior does not follow convention — a surprising default, a simulator-specific workaround, a non-obvious gotcha, or anything that works differently than a reader would reasonably expect — record it on the [Quirks & Known Issues](../known-issues.md) page.

That page is the canonical home for non-conventional behavior. Keep it alive: write the quirk down as you hit or introduce it, rather than leaving it in commit history or tribal memory. Use one `##` section per quirk, name it after the behavior, and say what a user or agent should do about it.

## Execution Contexts

Keep command execution rooted in explicit contexts rather than ambient `os.getcwd()`:

- `invocation_cwd`: the directory where the user ran `rb`. Use it to resolve relative CLI arguments before they become absolute.
- `command_root`: the directory containing the command's primary config file.
- `suite_dir`: the command root for per-suite flows such as `tests.yaml`, `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, and `power.yaml`.
- `artifact_dir`: the generated workspace for one command item, normally `suite_dir/artefacts/<name>`.

Config-driven commands should be rooted at their primary config file.
Generated artifacts should go under the command root.
External tools should run from their artifact directory.
Explicit CLI input and output paths should remain relative to `invocation_cwd`, matching normal shell behavior.

## Command Roots

Use these roots unless a command documents a narrower exception:

| Command | Command root | Artifact root | External tool CWD |
|---|---:|---:|---:|
| `test` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | compile: `<artifact>`; sim: `<artifact>` |
| `randtest` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | compile: `<artifact>`; sim: `<artifact>/run-NNNN` |
| `regression` | `dirname(regression.yaml)` | each suite's `<suite>/artefacts/<test>` | same as `test` per suite |
| `wave --resim` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | same as `test` |
| `synth` | `dirname(synth.yaml)` | `<suite>/artefacts/<synth>` | `<artifact>` |
| `cdc` | `dirname(cdc.yaml)` | `<suite>/artefacts/<cdc>` | `<artifact>` |
| `fpv` | `dirname(fpv.yaml)` | `<suite>/artefacts/<fpv>` | `<artifact>` |
| `pnr` | `dirname(pnr.yaml)` | `<suite>/artefacts/<pnr>` | `<artifact>` |
| `power` | `dirname(power.yaml)` | `<suite>/artefacts/<power>` | `<artifact>` |
| `hier --view dut` | `dirname(models.yaml)` | `<model_root>/artefacts/hier/<model>` | `<artifact>` |
| `hier --view tb` | `dirname(tests.yaml)` | `<suite>/artefacts/hier/<test-or-model>` | `<artifact>` |
| `axi-profile run` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile notebook` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile discover` | `dirname(models.yaml)` | `<model_root>/artefacts/axi/<model>` | `<artifact>` |
| `axi-profile gen-monitor` | `dirname(models.yaml)` | configured or explicit output; fallback artifact dir | `<artifact>` |
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
- `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, and `power.yaml` resolve their own fields relative to their config directory.

Do not let relative paths silently depend on where the user happened to invoke the command.
If a path is passed to an external tool, prefer an absolute path unless the value is intentionally artifact-relative.

## Artifact Layout

Generated outputs should live under `artefacts/<name>/` below the command root.
Repeated or randomized runs should use stable run directories such as `run-0001`, `run-0002`, and so on.
Convenience symlinks may point at the latest run, but they must not be the only durable location.

Compile-side generated files such as `run.f`, `compile.log`, builder outputs, and relative `builder-simv` paths belong in the per-test artifact root.
Simulation outputs for `randtest` belong in the per-run artifact directory to avoid side-file clobbering across iterations.

## Subprocesses

Every external tool invocation should pass an explicit `cwd`.
Use the command's artifact directory unless the command has a documented reason to run elsewhere.

Use `run_managed_process()` for long-running or tool-managed subprocesses so cleanup, timeout handling, and signal behavior stay consistent.
Plain `subprocess.run()` is acceptable only for short probes or helpers where lifecycle management is not needed; document that choice when it is not obvious.

## Dependencies

Classify every dependency using the buckets in [Installation](../install.md#dependency-types): required dependency, integrated tool, pluggable, or pluggable curated.
Use the classification to decide whether the dependency belongs in `pyproject.toml`, user install instructions, tool manifests, root-config schema, or command-specific docs.

Every new feature or dependency must update `docs/install.md` in the same PR.
The install page is the source of truth for feature-to-dependency mapping, required external tools, optional sub-dependencies, curated tools, and fork requirements.

Every external tool dependency must also be represented in `src/rtl_buddy/tool_manifest.py` unless there is a documented reason it cannot be checked.
The manifest is the source used by `rb tool-check`, `rb tool-check --required-for`, `rb tool-check --explain`, and runtime `tool_manifest.require()` errors.
Keep the manifest's `used_by`, `optional`, `minimum_version`, detector, install hint, and notes fields aligned with `docs/install.md`.

When a new feature adds or changes tool requirements, update `tests/test_tool_manifest.py` so `rb tool-check` reports the right readiness and install guidance.
Update [Tool Dependency Check](../concepts/tool-check.md) when manifest semantics, detector behavior, exit-code behavior, or command coverage changes.

Required Python dependencies should be kept minimal because they are installed for every user.
Prefer optional external tools for feature-specific functionality unless the dependency is needed by the core CLI, config loading, local docs access, or a command that cannot operate without it.

When adding an external tool integration, document:

- the command or feature that needs it;
- whether it is integrated, pluggable, or pluggable curated;
- any required version or fork;
- optional sub-dependencies such as coverage, rendering, or notebook extras;
- the concept page that explains build or setup details.
- the `rb tool-check --explain <tool>` hint users should see when it is missing.

## Logging

All runtime logging goes through `log_event()` in `src/rtl_buddy/logging_utils.py`.
Do not use direct `logger.info(f"...")` calls for runtime events.

Human mode converts events into readable text for `rtl_buddy.log` and console output.
Machine mode writes JSON Lines with the event name, fields, and human message.

When adding events:

- Use dotted names such as `compile.start`, `sim.timeout`, or `suite_config.load_failed`.
- Include structured fields that are stable and useful for agents.
- Add a dedicated human-message case for WARNING or ERROR events.
- Keep DEBUG and INFO events concise enough for machine logs.

## Error Handling

Fatal config and environment errors should log at ERROR and raise `FatalRtlBuddyError`.
The top-level command exits with code 2.

Per-test setup and filelist failures should become structured test results when the broader command can continue.
Use `FilelistError` for filelist failures caught by `TestRunner`.
Sweep and preproc failures should return a setup-failure string so the suite records `SetupFailResults`.

Do not use process-wide abort patterns for recoverable per-item failures.

## Validation

Let validation scale with risk:

- Docs-only edits: run frontmatter and MkDocs strict checks.
- CLI help changes: regenerate `docs/reference/cli.md` and run the generated-reference check.
- Path, artifact, or subprocess changes: add focused tests proving roots, generated paths, and subprocess `cwd`.
- Shared command-dispatch or config-loader changes: run the affected test module subset, then broaden if the change crosses command families.

Report skipped checks in the PR with the reason.

## Required Follow-Through

After meaningful `rtl_buddy` changes:

1. If CLI command names, flags, help text, or output behavior changed, regenerate `docs/reference/cli.md` with `uv run python scripts/gen_cli_reference.py`.
2. If a feature, command, optional extra, or external tool dependency changed, update `docs/install.md`.
3. If an external tool dependency changed, update `src/rtl_buddy/tool_manifest.py`, `tests/test_tool_manifest.py`, and `docs/concepts/tool-check.md` when tool-check behavior or coverage changes.
4. If docs changed, keep frontmatter valid and run the docs build. See [Documentation Guidelines](docs.md).
5. If behavior, YAML schema, version expectations, or validation workflows changed, update user docs and the bundled skill if agents rely on the behavior.
6. If release or packaging behavior changed, verify wheel inclusion rules and update downstream integrations after release.
7. If you discovered or introduced a quirk or non-conventional behavior, add an entry to `docs/known-issues.md`. Treat this as a default step, not an afterthought.
8. If the change is a `version/major` bump, add or update the `docs/migrations/vN-to-vM.md` page (and its `mkdocs.yml` nav entry) covering every breaking behavior change. See [Releases](#releases).

## Bundled Skill

The `rtl_buddy` agent skill ships inside the wheel at `src/rtl_buddy/skill/` and is installed by `rtl-buddy skill install`.
There is no separate source-of-truth skill repo.

Keep `src/rtl_buddy/skill/SKILL.md` short and agent-specific.
Prefer links to local docs commands over duplicating reference content.
Project-level installs are an override mechanism; the default install scope should remain user-level unless the policy is deliberately revisited.

## Issue Triage

Issues are classified along three axes that live on GitHub itself, not in this repo:

- **Type** — the org-level Issue Type: `Bug`, `Feature`, or `Docs`. Set once on every issue.
- **Priority** — the org-level Issue Field: `Urgent`, `High`, `Medium`, or `Low`. Reflects how soon the work should land, not how big it is.
- **Effort** — the org-level Issue Field: `High`, `Medium`, or `Low`. Optional; fill it in when the answer is non-obvious.

Type, Priority, and Effort are not labels.
They are GitHub Issue Fields configured at the `rtl-buddy` organization level and are queryable via the REST and GraphQL APIs.

Area is captured with `area/*` labels, kept consistent across all rtl-buddy repos.
The taxonomy is defined once in `.github/labels.json` and propagated to every repo with `.github/sync-labels.sh` — edit the JSON and re-run the script rather than creating labels by hand.
Pick one or more from the table below; an issue with no area label is fine for cross-cutting work but a single area is preferred when one fits.

| Label | Covers |
|---|---|
| `area/test` | `test`, `randtest`, `regression`, and the compile/sim runner pipeline |
| `area/wave` | waveform viewing and integration (surfer, WCP) |
| `area/cdc` | clock-domain crossing analysis (`rb cdc`) |
| `area/fpv` | formal property verification (`rb fpv`, sby plus commercial backends) |
| `area/abv` | assertion-based verification (SVA, properties) in sim |
| `area/mut` | mutation testing (`rb mut`) |
| `area/pd` | physical design: `synth`, `pnr`, `power`, and other implementation flows |
| `area/hier` | `hier` viewer and `rtl-buddy-view` integration |
| `area/axi-profile` | `axi-profile` discover, run, notebook, and monitor generation |
| `area/hub` | the hub server, marimo integration, hub event plumbing |
| `area/skill` | the bundled agent skill and `skill install` |
| `area/workflow` | spec-driven / end-to-end workflow orchestration |
| `area/config` | `root_config.yaml`, suite YAML loading, `filelist`, and model resolution |
| `area/tooling` | `tool-check`, `tool_manifest.py`, and external-tool integration |
| `area/infra` | CI workflows, packaging, release mechanics, dependencies, machine-mode logging, and the rtl-buddy CLI |

One extra label exists outside the area set:

- `discussion` — for issues that are scope or design conversations rather than tracked work.

The `version/patch`, `version/minor`, and `version/major` labels are reserved for PRs and drive the release workflow.
Do not apply them to issues.

## Milestones

Use milestones to group issues and PRs that share a long-running, multi-issue effort.
Single-issue work does not need a milestone.

Name milestones by theme, not by version — for example `Hub Phase 3` or `ABV in sim`.
Release impact already lives on the `version/*` labels and PyPI tags, so milestones should carry orthogonal information.

Open a milestone when the effort is scoped enough to enumerate its first few issues, and close it when the last constituent issue is closed.
Roll over remaining work into a follow-up milestone rather than leaving the original open indefinitely.

## Releases

Stable releases are produced by merging to `main` with one of the `version/patch`, `version/minor`, or `version/major` labels.
Pre-releases are cut from feature branches by workflow dispatch and should not be produced by merging pre-release branches into `main`.

Docs publishing, PyPI publishing, and downstream template updates depend on that sequence.
Do not push a template pin for an unreleased `rtl_buddy` version.

Every `version/major` bump must ship a migration page at `docs/migrations/vN-to-vM.md`, added to the `mkdocs.yml` nav, before merge.
The page documents every breaking behavior change — moved outputs, changed defaults, removed or renamed config fields, and any contract that downstream projects or hook scripts depend on — and tells readers what to update.
A recurring failure mode is a silent contract change buried in a PR description; the migration page is where it must live so users and agents find it.
