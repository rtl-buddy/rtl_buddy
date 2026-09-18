---
description: Resolve RTL Buddy command roots, configuration paths, logs, artefacts, hook paths, and concurrent-run conflicts.
---

# Execution Context

Config-driven commands anchor generated work to their primary configuration file, regardless of the directory from which you invoke `rb`.

## Resolve command paths

RTL Buddy uses three anchors:

| Anchor | Meaning |
| --- | --- |
| `invocation_cwd` | The shell directory where `rb` was invoked |
| `command_root` | The directory containing the command's primary config |
| `artifact_root` | `<command_root>/artefacts/`, or `<command_root>/artefacts/.runs/<tag>/` under `--run-tag` |

Generated artefacts, builder scratch, and `rtl_buddy.log` use the command root. Explicit CLI input and output paths use normal shell semantics and are resolved from `invocation_cwd`.

For example:

```bash
cd repo/design/block
rb test basic -c ../../verif/block/tests.yaml
```

The test runs under `repo/verif/block/artefacts/basic/` and writes `repo/verif/block/rtl_buddy.log`. An explicit output such as `rb filelist model out.f ...` still writes `out.f` in `repo/design/block`.

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

Relative paths declared in YAML resolve from the file that owns them:

- Regression manifests resolve their listed suite or flow configs from the manifest directory.
- `tests.yaml` resolves testbench filelists, hook scripts, and suite assets from the suite directory.
- `models.yaml` resolves model filelist entries from its own directory.
- Flow configs such as `synth.yaml`, `fpv.yaml`, `pnr.yaml`, and `power.yaml` resolve their fields from their own directory.

Absolute paths pass through unchanged. A YAML path never changes meaning based on `invocation_cwd`.

## Write hook outputs safely

In `sweep` and `preproc` scripts, use the supplied `suite_dir` and `artifact_dir` variables. The process working directory remains `invocation_cwd`.

```python
out = os.path.join(artifact_dir, "gen.sv")  # correct
out = os.path.join(os.getcwd(), "gen.sv")  # wrong: invocation cwd
```

The configured `postproc` script is not currently executed; built-in post-processing determines results. See [Hook execution context](plugins.md#handle-hook-execution-context).

## Namespace concurrent runs

`rb test`, `rb randtest`, `rb regression`, and `rb graph results` accept `--run-tag <name>`. The tag moves that invocation's whole artefact tree under `artefacts/.runs/<tag>/`:

| Path | Without `--run-tag` | With `--run-tag sim-a` |
| --- | --- | --- |
| Per-test artefacts | `<suite>/artefacts/<test>[/run-NNNN]` | `<suite>/artefacts/.runs/sim-a/<test>[/run-NNNN]` |
| Result envelope | `<suite>/artefacts/<test>/result.json` | `<suite>/artefacts/.runs/sim-a/<test>/result.json` |
| Tree lock | `<suite>/artefacts/.rtl-buddy.lock` | `<suite>/artefacts/.runs/sim-a/.rtl-buddy.lock` |
| Command log | `<command_root>/rtl_buddy.log` | `<suite>/artefacts/.runs/sim-a/rtl_buddy.log` |
| Dispatch head outputs | `<suite>/artefacts/.dispatch/` | `<suite>/artefacts/.runs/sim-a/.dispatch/` |
| Results overlay | `<root>/artefacts/graph/results-overlay.json` | `<root>/artefacts/.runs/sim-a/graph/results-overlay.json` |
| Shared builds | `<suite>/artefacts/.shared-builds/` | unchanged — shared across tags |
| `graph.json` | `<root>/artefacts/graph/graph.json` | unchanged — the design graph is not per-run |

Run two tiers at once, one per simulator, and convert each run's results separately:

```bash
rb -B verilator regression --run-tag verilator &
rb -B icarus regression --run-tag icarus &
wait
rb graph results --run-tag verilator
rb graph results --run-tag icarus
```

A tag must be a single safe path segment: letters, digits, `.`, `_`, and `-`, at most 64 characters. `.`, `..`, and anything containing a path separator are rejected before any directory is created. The directory is dot-prefixed (`.runs`) so a tag can never be confused with a test of the same name, and so every existing reader of the tree — the results-overlay scan, the `+incdir+` walk prune, the artefact clearers — keeps skipping it.

Shared builds stay shared on purpose. A build directory is keyed on the compile fingerprint, including the toolchain executable, so two simulators already get separate `obj_dir`s and two tags that compile the same thing reuse one build instead of paying for it twice.

Without `--run-tag` nothing moves. The flat layout, the lock path, the log location, and the result envelope bytes are exactly what they were.

## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holding PID, command, and start time.

Wait for the first process to finish or terminate that process if it is stale. The kernel releases the lock on normal exit, crash, or kill; the metadata file itself does not need removal. Listing commands do not take the lock.

The lock covers the entire artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Commands using different artefact roots can run concurrently — which is what [`--run-tag`](#namespace-concurrent-runs) gives two runs of the same suite. Two runs naming the same tag still contend.

This protection is host-local. Do not run the same suite concurrently from multiple machines on a shared filesystem unless the environment provides equivalent coordination.

## Find the log

Read `<command_root>/rtl_buddy.log`. It is plain text by default and JSON Lines under `--machine`. For regressions, inspect the relevant suite log for test details and the manifest-root log for the final summary.

The log belongs to the flow that wrote it. A file log is opened for writing and a process's first open of it truncates it, so the file holds one run and not an accumulation of every run before it. That is why `--run-tag` moves the log into the tagged artefact root: two concurrent runs of one suite sharing `<suite>/rtl_buddy.log` would erase each other's record. The commands that take no lock also attach no file log — `rb phys`, `rb cov`, the `rb graph` read verbs, `rb xplr`, and `--list` on any flow command — because they write nothing else, and opening the log would destroy the record of the run being read about. They report on the console and, under `--machine`, in the JSON result on stdout. See [Agent Use](../agents.md#know-which-commands-write-a-log).
