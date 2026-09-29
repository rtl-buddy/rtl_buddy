---
description: Check external-tool availability and versions, diagnose blocked subcommands, and gate CI with rb tool-check.
---

# Tool dependency check

`rb tool-check` reports which external tools are detected and which `rb` subcommands are blocked by a missing or outdated dependency. It works without a project; inside one, it applies the paths and version pins from `root_config.yaml`.

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

- **Tools:** canonical name, status (`ok`, `missing`, `outdated` or `unsupported`), detected version, resolved path, minimum version and whether the tool is optional. `unsupported` marks a tool newer than the range rtl-buddy is built against (pyslang 12 and later, for example) and blocks commands like `outdated` does.
- **Subcommand readiness:** each `rb` command and the dependencies blocking it. An optional feature does not make unrelated commands unready.

A tool can be optional globally yet required by an optional command: pyslang does not block the core install, but blocks `elab` and `elab-regression`. Optional tools are shown by default; `--no-include-optional` hides them.

- `--required-for <subcommand>` is a focused preflight for one command.
- `--explain <tool>` prints the detected state, the commands using the tool, its optional binaries and platform install hints. Use it after a wrapper reports a missing dependency. Output always uses the canonical name.
- Tool names accept aliases in `--explain` and in runtime dependency checks: `rtl-buddy-sch` resolves to `rtl-buddy-view`. An unknown name with `--explain` exits 1. Under `--machine` the response lists the known tool names and the alias mapping.

## Read optional binaries

A tool may list `Optional binaries (not required; not detected as this tool)`, each with what it buys. They never satisfy detection, supply the probed version or change a status: a host with `scontrol` but no `sbatch` reports slurm `missing`.

Slurm's `scontrol` is the main example:

- `scontrol show config` gives dispatch the cluster's `MaxArraySize` and `max_array_tasks`, so it can split a group too large for one job array. Without it, set `cfg-dispatch.max-array-size` and, if the cluster caps tasks per array lower, `cfg-dispatch.max-array-tasks`.
- `scontrol update JobId=<id> Dependency=` starts a compile key's simulation jobs as soon as that key is built. The call runs on the compute node executing the build job, so a passing check on the submit host does not prove it works there. Without it, simulation jobs wait for the whole build job.

## Check in-process readers

The report ends with an `In-process readers` section for behavior that changes without an external binary:

```text
In-process readers
----------------------------------------------------------------------
  constraint reader: tcl (Tcl 9.0.3)
```

`constraint reader` is the backend used to read SDC/XDC files:

- `tcl`: a safe Tcl interpreter in a short-lived worker process, reported with the Tcl version it found.
- `tokenizer`: a stdlib-only word splitter used when no worker can start an interpreter, typically a Python without `_tkinter`. It does not evaluate `$variables` or `[expr ...]`.

`RTL_BUDDY_CONSTRAINT_READER=tokenizer|tcl` pins the choice. The JSON payload carries it as `readers.constraints`, without the version. See [How the SDC is read](synthesis.md#how-the-sdc-is-read).

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

Prefer `rb --machine tool-check` for agents; `--format json` prints the same payload bare.

- The payload has `tools`, `subcommands` and `exit_code`. `exit_code` is the would-be enforced result even when the informational command exits 0.
- Each `tools` entry carries `status`, `version`, `path`, `optional` and, when declared, `minimum_version`.
- A tool whose subcommands need different versions also carries `subcommand_minimum_versions`. The matching `subcommands` entry reports `outdated` with a `minimum_versions` map naming the missed floor, while the tool stays `ok`. `rtl-buddy-view` is the built-in case: 0.3.0 is enough for `rb hier`, `rb hier-query` and `rb hub`, but `rb graph` needs 0.4.0.
- Optional binaries have no field. `rb --machine tool-check --explain <tool>` puts the human explanation, optional binaries included, in `instructions`.

## Apply project configuration

When a project is discoverable, tool-check merges the built-in manifest with `root_config.yaml`:

- `cfg-verible` and the active `cfg-surfer` entry add preferred detectors; `PATH` remains a fallback. Absolute paths work.
- `cfg-tools` overrides minimum versions. A platform-qualified entry applies only to the matching configured OS and beats an unqualified one.
- `cfg-fpv-tools[*].opts.solver-versions` sets solver version expectations. FPV runs check exact equality; tool-check shows a mismatch as `outdated`.
- Other `cfg-*-tools` blocks do not select a detector, because each flow picks its entry at run time. A flow's pinned `tool:` path is used when that flow runs.

Without `root_config.yaml`, built-in detectors and version floors apply. Detected versions are cached in `${XDG_CACHE_HOME:-~/.cache}/rtl_buddy/tool_versions.json`, keyed by binary path and modification time. `--no-probe-versions` skips probing for a faster presence-only check; versions show as unknown.

## Understand the manifest

`src/rtl_buddy/tool_manifest.py` is the source for both reports and runtime dependency errors. Each tool declares its canonical name and aliases, required binaries, ordered detection methods, version probe and minimum, install hints, dependent subcommands, whether it is optional, and its optional binaries.

- `binaries` is an any-of list: the first name found on `PATH` or in a configured vendor directory makes the tool detected, and that path is used for the version probe. A helper that is not enough by itself goes in `optional_binaries` (binary name to what it buys), which only `--explain` reads.
- The first successful detector wins. Detectors cover `PATH`, configured absolute or vendor paths, Python packages and sibling Python distributions. Name or alias collisions are rejected.

Runtime wrappers use the same manifest and print the same recovery hint:

```text
<tool> not found — run `rb tool-check --explain <tool>` for install instructions
```

`rb tool-check` diagnoses and explains; it does not install tools. See [YAML formats](../reference/yaml.md#root_configyaml) and the [CLI reference](../reference/cli.md).
