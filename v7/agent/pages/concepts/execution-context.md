---
description: Resolve RTL Buddy command roots, configuration paths, logs, artefacts, hook paths, and concurrent-run conflicts.
---

# Execution Context

Config-driven commands anchor generated work to their primary configuration file, whichever directory you run `rb` from. This page tells you where outputs land, how YAML paths resolve, and how to run the same suite twice at once.

## Resolve command paths

RTL Buddy uses three anchors:

| Anchor | Meaning |
| --- | --- |
| `invocation_cwd` | The shell directory where `rb` was invoked |
| `command_root` | The directory containing the command's primary config |
| `artifact_root` | `<command_root>/artefacts/`, or `<command_root>/artefacts/.runs/<tag>/` under `--run-tag` |

Artefacts, builder scratch and `rtl_buddy.log` use the command root. Explicit CLI input and output paths use normal shell semantics and resolve from `invocation_cwd`.

```bash
cd repo/design/block
rb test basic -c ../../verif/block/tests.yaml
```

This runs under `repo/verif/block/artefacts/basic/` and writes `repo/verif/block/rtl_buddy.log`. An explicit output such as `rb filelist model out.f ...` still writes `out.f` in `repo/design/block`.

## Find each command root

| Command | Command root | Artefact or tool directory |
| --- | --- | --- |
| `test`, `randtest`, `wave` | Directory containing `tests.yaml` | `artefacts/<test>[/run-NNNN]` |
| `regression` | Directory containing `regression.yaml` | Each suite's own artefact tree |
| `synth`, `fpv`, `pnr`, `power` | Directory containing that flow's YAML | `artefacts/<run>` |
| `mut` | Directory containing `mut.yaml` | `artefacts/mut/<campaign>` |
| `hier --view dut` | Directory containing `models.yaml` | `artefacts/hier/<model>` |
| `hier --view tb` | Directory containing `tests.yaml` | `artefacts/hier/<model>/tb/<testbench>` |
| `axi-profile run` | Directory containing `tests.yaml` | `artefacts/axi/<test>` |
| `axi-profile discover` | Directory containing `models.yaml` | `artefacts/axi/<model>` |
| `filelist`, `saif` | Config root for reads; shell CWD for explicit output | Explicit output path |
| `hub` | Project root | `.rtl-buddy/` |

External tools run inside the listed artefact directory. A regression re-anchors each suite's outputs and log to that suite, then writes its final log and merged outputs beside `regression.yaml`.

## Resolve config paths

A relative path in YAML resolves from the file that declares it, never from `invocation_cwd`. Absolute paths pass through unchanged.

- Regression manifests resolve their suite and flow configs from the manifest directory.
- `tests.yaml` resolves testbench filelists, hook scripts and suite assets from the suite directory.
- `models.yaml` resolves model filelist entries from its own directory.
- `synth.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml` and similar flow configs resolve their fields from their own directory.

## Write hook outputs safely

`sweep` and `preproc` scripts run with the working directory at `invocation_cwd`. Build output paths from the supplied `suite_dir` and `artifact_dir` variables:

```python
out = os.path.join(artifact_dir, "gen.sv")  # correct
out = os.path.join(os.getcwd(), "gen.sv")  # wrong: invocation cwd
```

A configured `postproc` script is accepted but not executed; built-in post-processing decides results. See [Hook execution context](plugins.md#handle-hook-execution-context).

## Namespace concurrent runs

`rb test`, `rb randtest`, `rb regression` and `rb graph results` accept `--run-tag <name>`, which moves that invocation's whole artefact tree under `artefacts/.runs/<tag>/`. Use it to run the same suite twice at once, for example one run per simulator:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
rb graph results --run-tag verilator
rb graph results --run-tag icarus
```

| Path | Without `--run-tag` | With `--run-tag sim-a` |
| --- | --- | --- |
| Per-test artefacts | `<suite>/artefacts/<test>[/run-NNNN]` | `<suite>/artefacts/.runs/sim-a/<test>[/run-NNNN]` |
| Result envelope | `<suite>/artefacts/<test>/result.json` | `<suite>/artefacts/.runs/sim-a/<test>/result.json` |
| Tree lock | `<suite>/artefacts/.rtl-buddy.lock` | `<suite>/artefacts/.runs/sim-a/.rtl-buddy.lock` |
| Command log | `<command_root>/rtl_buddy.log` | `<suite>/artefacts/.runs/sim-a/rtl_buddy.log` |
| Dispatch head outputs | `<suite>/artefacts/.dispatch/` | `<suite>/artefacts/.runs/sim-a/.dispatch/` |
| Results overlay | `<root>/artefacts/graph/results-overlay.json` | `<root>/artefacts/.runs/sim-a/graph/results-overlay.json` |
| Shared builds | `<suite>/artefacts/.shared-builds/` | unchanged, shared across tags |
| `graph.json` | `<root>/artefacts/graph/graph.json` | unchanged, not per-run |

- A tag is one path segment of letters, digits, `.`, `_` and `-`, at most 64 characters. `.`, `..` and anything containing a path separator are rejected before any directory is created.
- Shared builds stay shared. The build key includes the toolchain executable, so two simulators get separate `obj_dir`s, and two tags compiling the same thing reuse one build.
- Under a tag the compile runs one directory deeper, so a relative path in a builder mode's `compile-time` opts names a different file. Write project files as `${RTL_BUDDY_PROJECT_ROOT}/design/waive.vlt`; see [Simulator builders](../reference/yaml.md#simulator-builders).
- Without `--run-tag` the layout is the flat one in the left column.

## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holder's PID, command, start time and host. Listing commands do not lock.

- Wait for the first process to finish, or terminate it if it is stale.
- The lock is released on a clean exit, a tool failure and `Ctrl-C` (exit 130). The kernel also releases it on crash or kill, so the metadata file never needs removal.
- A lock naming this host and a dead process, or a PID since reused by another process, is reclaimed by the next run, which logs `artifact_lock.reclaimed`.
- A lock recorded on another host is never judged by PID. On a shared filesystem, clear a cross-machine lock deliberately.
- The lock covers the whole artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Runs with different artefact roots do not, which is what [`--run-tag`](#namespace-concurrent-runs) provides. Two runs naming the same tag still contend.
- The lock is host-local. Do not run the same suite from several machines on a shared filesystem without your own coordination.

## Find the log

Read `<command_root>/rtl_buddy.log`: plain text by default, JSON Lines under `--machine`. For a regression, read the suite's log for test detail and the manifest-root log for the final summary.

Each run's first open of the log truncates it, so the file holds one run. That is why `--run-tag` moves the log into the tagged artefact root: concurrent runs sharing one file would erase each other's record.

Commands that take no lock attach no file log: `rb phys`, `rb cov`, the `rb graph` read verbs, `rb xplr`, and `--list` on any flow command. They report on the console and, under `--machine`, in the JSON result. See [Agent Use](../agents.md#know-which-commands-write-a-log).
