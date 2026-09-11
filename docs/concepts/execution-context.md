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

## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holding PID, command, and start time.

Wait for the first process to finish or terminate that process if it is stale. The kernel releases the lock on normal exit, crash, or kill; the metadata file itself does not need removal. Listing commands do not take the lock.

The lock covers the entire artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Commands using different artefact roots can run concurrently.

This protection is host-local. Do not run the same suite concurrently from multiple machines on a shared filesystem unless the environment provides equivalent coordination.

## Run two regressions in one checkout

Two runs that have no serial dependency — a nightly gating on two simulators, say — still collide, because the artefact tree carries no run identity and the lock above covers all of it.

Give each run a `--run-tag`:

```bash
rb --run-tag vcs regression -c regression.yaml --dispatch slurm &
rb --run-tag verilator regression -c regression.yaml --dispatch slurm &
wait
rb --run-tag vcs graph results
rb --run-tag verilator graph results
```

Each run writes `<suite>/artefacts/.runs/<tag>/` — its own per-test directories, its own `.shared-builds`, its own `.dispatch` envelopes, its own `cov_dir`, its own `rtl_buddy.log`, its own lock. Nothing is shared, including the compiles: a second head may legitimately rebuild a compile key the first one's dispatched jobs are gated on, and a gated job that cannot validate the build it was gated on fails rather than recompiling.

The tag rides the dispatched job's command line, so scheduler jobs write back into the tree their head owns.

Without `--run-tag`, every path and the lock are exactly what they were.

Two bounds:

- `--run-tag` is accepted by `test`, `randtest`, `regression` and every `graph` subcommand except `graph build`. Everything else refuses it: those commands build `artefacts/<name>` directly, so honouring a tag would move the lock without moving the outputs and two tagged runs would believe they were isolated while writing one directory. `rb graph build` is refused for the same reason, since `artefacts/graph/graph.json` describes the design rather than a run.
- A tagged run locks `artefacts/.runs/<tag>/`, so it no longer excludes a concurrent `rb synth` in the same suite the way an untagged run does. Their subtrees are disjoint, but the mutual exclusion an untagged run gets is not there.
- A tagged run writes no `<suite>/test.log`, `test.err` or `test.randseed` latest-run link. Two live runs cannot both be "latest", and per-tag link names would be fingerprinted as compile inputs by a suite reached through `+incdir+.`. Read the file inside that run's own artefact directory instead.

`rb graph results --run-tag <tag>` scans that run's trees and writes its overlay to `artefacts/.runs/<tag>/graph/results-overlay.json`. `rb graph query`, `graph path` and `graph explain` take the same tag and read that overlay. `graph.json` is still read from `artefacts/graph/` — the graph describes the design, not the run, so it is built once for all tags.

Coverage follows the tag too: `cov_dir`, the merged HTML and the Coverview zips are published inside that run's tree, and `graph results --run-tag` joins only that run's coverage rather than whichever manifest is newest in the checkout.

`rb cov` does not take the tag. Point it at the run you want with `--cov-dir <suite>/artefacts/.runs/<tag>/cov_dir`.

## Find the log

Read `<command_root>/rtl_buddy.log`. It is plain text by default and JSON Lines under `--machine`. For regressions, inspect the relevant suite log for test details and the manifest-root log for the final summary.
