---
description: Check external-tool availability and versions, diagnose blocked subcommands, and gate CI with rb tool-check.
---

# Tool dependency check

`rb tool-check` reports detected external tools and the `rb` subcommands blocked by missing or outdated dependencies. It works without a project, and applies project-specific paths and version pins when it discovers `root_config.yaml`.

## Check the environment

```bash
rb tool-check                         # informational text report
rb tool-check --required-for fpv      # only FPV dependencies; enforced
rb tool-check --explain surfer        # status and install instructions
rb tool-check --strict                # gate all required tools
rb tool-check --format json           # bare JSON for scripts
rb --machine tool-check               # standard machine envelope
```

Optional tools appear by default. Use `--no-include-optional` to hide them. A tool may be optional globally but required by a command that is itself optional: pyslang does not block the core install, but it does block `elab` and `elab-regression`. The report contains:

- **Tools:** canonical name, `ok` / `missing` / `outdated`, detected version, resolved path, minimum version, and optional status.
- **Subcommand readiness:** each declared `rb` command and the dependencies that block it. An optional feature does not make unrelated commands unready.

Use `--required-for <subcommand>` for a focused preflight. Use `--explain <tool>` after a wrapper reports a missing dependency; it prints the detected state, commands that use the tool, any optional binaries, and platform-specific install hints.

A tool that declares optional binaries lists them under `Optional binaries (not required; not detected as this tool)`, each with what it buys. They enrich the tool without being part of it: they never satisfy detection, never supply the probed version, and never change a `ok` / `missing` / `outdated` status. Slurm's `scontrol` is the example — `scontrol show config` supplies the cluster's `MaxArraySize`, so dispatch can split a resource group too large for one job array, and a submit host without it dispatches normally once `cfg-dispatch.max-array-size` is set. Reading the absence of an optional binary as a missing tool, or its presence as a present one, is exactly the confusion the separate section exists to prevent: a host with `scontrol` but no `sbatch` reports slurm `missing`.

Aliases are accepted by `--explain` and runtime dependency checks. Output always uses the canonical tool name. For example, `rtl-buddy-sch` resolves to `rtl-buddy-view`; an unknown-name machine response includes the known names and alias mapping.

## Gate scripts and CI

Exit behavior depends on the invocation:

| Invocation | Exit | Meaning |
|---|---:|---|
| `rb tool-check` | 0 | Informational, regardless of tool state |
| `rb tool-check --strict` | 0 | All required tools are ready |
| `rb tool-check --strict` | 1 | A required tool is missing or outdated |
| `rb tool-check --required-for <subcommand>` | 0 | That command's required tools are ready |
| `rb tool-check --required-for <subcommand>` | 2 | That command is blocked |

`--required-for` implies enforcement. Optional dependencies do not fail the global `--strict` check, but they do fail a focused check for a command that declares them required.

The JSON payload contains `tools`, `subcommands`, and `exit_code`. Each `tools` entry carries `status`, `version`, `path`, `optional`, and `minimum_version` when one is declared. Optional binaries are deliberately absent from it: they are documentation of what a tool can additionally use, not a state anything can gate on, so machine consumers see no field for them. `rb --machine tool-check --explain <tool>` mirrors the human explanation verbatim in the payload's `instructions` field, which is where they do appear. `exit_code` reports the would-be enforced result even when the informational command itself exits 0. `rb --machine tool-check` wraps the same payload in the standard machine envelope; prefer that form for agents.

Example focused CI gate:

```bash
rb tool-check --required-for fpv --strict || {
  echo "rb fpv is not ready"
  exit 1
}
```

## Apply project configuration

When a project is discoverable, tool-check reconciles the built-in manifest with `root_config.yaml`:

- `cfg-verible` and the active `cfg-surfer` entry add preferred detectors while retaining `PATH` fallback. Absolute paths are supported.
- `cfg-tools` overrides minimum versions. Platform-qualified entries apply only to the matching configured OS and take precedence over unqualified entries.
- `cfg-fpv-tools[*].opts.solver-versions` supplies solver version expectations. Runtime FPV checks exact equality; tool-check presents a mismatch as outdated.
- Other `cfg-*-tools` blocks do not select a detector because each flow chooses its entry at run time. A flow's pinned `tool:` path is honored when that flow runs.

Without `root_config.yaml`, built-in detectors and version floors apply.

Detected versions are cached at `${XDG_CACHE_HOME:-~/.cache}/rtl_buddy/tool_versions.json`, keyed by binary path and modification time. Use `--no-probe-versions` for a faster presence-only check; versions then display as unknown.

## Understand the manifest

`src/rtl_buddy/tool_manifest.py` is the source of truth for both reports and runtime dependency errors. Each tool declares its canonical name and aliases, its required binaries, ordered detection methods, version probe and minimum, install hints, dependent subcommands, whether it is optional, and any optional binaries.

`binaries` is the tool's required core, and it is an any-of list: the first name found on `PATH` (or in a configured vendor directory) makes the tool detected, and that resolved path is substituted into the version probe. A binary that does not by itself make the tool usable therefore does not belong there — listing one would let a host missing every real command report `ok`, version-probed through the wrong executable. Such helpers go in `optional_binaries`, a mapping of binary name to what it buys, which only `--explain` reads.

The first successful detector wins. Detectors cover `PATH`, configured absolute or vendor paths, Python packages, and sibling Python distributions. Manifest construction rejects name or alias collisions.

Runtime wrappers call the same manifest and produce a consistent recovery hint:

```text
<tool> not found — run `rb tool-check --explain <tool>` for install instructions
```

`rb tool-check` diagnoses and explains dependencies; it does not install tools or accept project-defined tool specifications. Projects may override known tool paths and versions through `root_config.yaml`. See [YAML formats](../reference/yaml.md#root_configyaml) and the [CLI reference](../reference/cli.md).
