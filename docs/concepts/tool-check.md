---
description: Check external-tool availability and versions, diagnose blocked subcommands, and gate CI with rb tool-check.
---

# Tool dependency check

`rb tool-check` reports which external tools are detected and which `rb` subcommands are blocked by a missing or outdated dependency. It works without a project. Inside one, it applies the paths and version pins from `root_config.yaml`. It diagnoses and explains; it does not install tools.

## Check the environment

```bash
rb tool-check                         # informational text report
rb tool-check --required-for fpv      # only FPV dependencies; enforced
rb tool-check --explain surfer        # status and install instructions
rb tool-check --strict                # gate all required tools
rb tool-check --format json           # bare JSON for scripts
rb --machine tool-check               # standard machine envelope
```

The report has two parts:

- **Tools:** name, status (`ok`, `missing`, `outdated` or `unsupported`), detected version, path, minimum version and whether the tool is optional. `unsupported` marks a tool newer than the range rtl-buddy is built against (pyslang 12 and later, for example) and blocks commands as `outdated` does.
- **Subcommand readiness:** each `rb` command and the dependencies blocking it. An optional tool can still block the commands that need it: pyslang does not block the core install, but blocks `elab` and `elab-regression`.

Optional tools are shown by default; `--no-include-optional` hides them. `--required-for <subcommand>` is a preflight for one command. `--explain <tool>` prints the detected state, the commands using the tool and platform install hints; use it after a wrapper reports a missing dependency. Aliases work, so `rtl-buddy-sch` resolves to `rtl-buddy-view`. An unknown name exits 1.

Versions are cached per binary path and modification time. `--no-probe-versions` skips probing for a faster presence-only check; versions show as unknown.

## Optional binaries

A tool may list `Optional binaries (not required; not detected as this tool)`, each with what it buys. They never change a tool's status: a host with `scontrol` but no `sbatch` reports slurm `missing`.

Slurm's `scontrol` is the main case:

- Without it, dispatch cannot read the cluster's array limits. Set `cfg-dispatch.max-array-size` and, if the cluster caps tasks per array lower, `cfg-dispatch.max-array-tasks`.
- Without `scontrol update`, simulation jobs wait for the whole build job instead of starting as soon as their build finishes. The call runs on the compute node, so a passing check on the submit host does not prove it works there.

## In-process readers

The report ends with an `In-process readers` section:

```text
In-process readers
----------------------------------------------------------------------
  constraint reader: tcl (Tcl 9.0.3)
```

`constraint reader` is the backend that reads SDC/XDC files:

- `tcl`: a safe Tcl interpreter in a short-lived worker process.
- `tokenizer`: a word splitter used when no interpreter can start, typically a Python without `_tkinter`. It does not evaluate `$variables` or `[expr ...]`.

`RTL_BUDDY_CONSTRAINT_READER=tokenizer|tcl` pins the choice. See [How the SDC is read](synthesis.md#how-the-sdc-is-read).

## Gate scripts and CI

| Invocation | Exit | Meaning |
|---|---:|---|
| `rb tool-check` | 0 | Informational, regardless of tool state |
| `rb tool-check --strict` | 0 | All required tools are ready |
| `rb tool-check --strict` | 1 | A required tool is missing, outdated or unsupported |
| `rb tool-check --required-for <subcommand>` | 0 | That command's required tools are ready |
| `rb tool-check --required-for <subcommand>` | 2 | That command is blocked |

`--required-for` implies enforcement. Optional dependencies do not fail the global `--strict` check, but do fail a focused check for a command that requires them.

```bash
rb tool-check --required-for fpv --strict || {
  echo "rb fpv is not ready"
  exit 1
}
```

## Read the JSON payload

Agents should use `rb --machine tool-check`; `--format json` prints the same payload bare.

- The payload has `tools`, `subcommands` and `exit_code`. `exit_code` is the result an enforced run would give, even when the informational command exits 0.
- Each `tools` entry has `status`, `version`, `path`, `optional` and, when declared, `minimum_version`.
- When subcommands of one tool need different versions, the tool stays `ok` and the affected `subcommands` entry reports `outdated` with the missed minimum. `rtl-buddy-view` 0.3.0 is enough for `rb hier`, `rb hier-query` and `rb hub`, but `rb graph` needs 0.4.0.
- `rb --machine tool-check --explain <tool>` returns the human explanation, optional binaries included, in `instructions`. An unknown tool lists the known names and aliases.

## Apply project configuration

Inside a project, tool-check merges the built-in tool list with `root_config.yaml`:

- `cfg-verible` and the active `cfg-surfer` entry add preferred locations; `PATH` remains a fallback. Absolute paths work.
- `cfg-tools` overrides minimum versions. An entry qualified by platform applies only to that OS and beats an unqualified one.
- `cfg-fpv-tools[*].opts.solver-versions` sets solver versions. FPV runs require exact equality; tool-check shows a mismatch as `outdated`.
- Other `cfg-*-tools` blocks are not consulted, because each flow picks its entry at run time.

Without `root_config.yaml`, built-in locations and version floors apply. See [YAML formats](../reference/yaml.md#root_configyaml) and the [CLI reference](../reference/cli.md).

## Troubleshooting

| Symptom | Action |
|---|---|
| ``<tool> not found — run `rb tool-check --explain <tool>` for install instructions`` | Run the suggested command and install the tool |
| Tool `missing` although installed | Put it on `PATH`, or set its path in `root_config.yaml` (see above) |
| Tool `outdated` or `unsupported` | Install a version in the supported range, or adjust `cfg-tools` minimums |
| Command blocked by an optional tool | Install that tool, or do not run the command |
| `--explain` exits 1 | The tool name is unknown; use a name or alias from `rb tool-check` |
| `--required-for` exits 2 | The named command is blocked; read the readiness section |
| Slurm reports `missing` with only `scontrol` present | Install `sbatch`; `scontrol` alone is not detected as Slurm |
