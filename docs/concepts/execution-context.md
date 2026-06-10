---
description: Where rtl_buddy puts artifacts and logs, and how relative paths are resolved when you invoke rb from outside the suite directory.
---

# Execution Context

`rtl_buddy` commands always anchor their work on the **primary config file** (`tests.yaml`, `synth.yaml`, `cdc.yaml`, …), not on the directory you happened to run `rb` from. This means the same command produces the same artifact layout regardless of where you invoked it.

If you've ever run `rb` from a design directory and ended up with a stray `verif/artefacts/` or `rtl_buddy.log` next to your RTL sources, this page explains why that no longer happens.

## The three anchors

Every command has three paths it cares about:

| Anchor | What it is |
| --- | --- |
| `invocation_cwd` | The directory you ran `rb` from — your shell's working directory. |
| `command_root` | The directory containing the command's primary config file. |
| `artifact_root` | Where the artifact tree lives. Defaults to `command_root/artefacts/`. |

And one rule that ties them together:

> **Config-driven commands anchor to their primary config. Explicit CLI input/output paths anchor to your shell's cwd.**

Generated outputs (`artefacts/<name>/`, `rtl_buddy.log`, builder scratch) go under the command root. Things you typed on the command line (`-o out.svg`, an output filelist path) follow normal shell semantics — they land where you told them to.

## A worked example

Suppose your repo looks like this:

```text
repo/
├── design/<block>/        # RTL sources
└── verif/<block>/
    └── tests.yaml
```

You're sitting in `repo/design/<block>` (looking at the RTL) and want to run a quick test. You point `rb` at the suite with `-c`:

```bash
cd repo/design/<block>
rb test basic -c ../../verif/<block>/tests.yaml
```

Here is what each anchor resolves to:

- `invocation_cwd` = `repo/design/<block>`
- `command_root` = `repo/verif/<block>` (`dirname(tests.yaml)`)
- `artifact_root` = `repo/verif/<block>/artefacts`

So the test creates `repo/verif/<block>/artefacts/basic/...` and `repo/verif/<block>/rtl_buddy.log`. Nothing lands in `design/<block>`.

If you'd passed an explicit output:

```bash
rb filelist <model> out.f -c ../../verif/<block>/models.yaml
```

The filelist lands at `repo/design/<block>/out.f` (your shell's cwd) because `out.f` is a user-supplied output path. The orchestration log still lands at `dirname(models.yaml)/rtl_buddy.log`.

## Per-command mapping

| Command | command_root | artifact_root | External tool CWD |
| --- | --- | --- | --- |
| `test`, `randtest` | `dirname(tests.yaml)` | `<command_root>/artefacts` | `<artifact>/<test>[/run-NNNN]` |
| `regression` | `dirname(regression.yaml)` | each suite's own `artefacts/` | per-suite, same as `test` |
| `wave`, `wave --resim` | `dirname(tests.yaml)` | `<command_root>/artefacts` | `<artifact>/<test>` |
| `synth` | `dirname(synth.yaml)` | `<command_root>/artefacts` | `<artifact>/<synth>` |
| `cdc` | `dirname(cdc.yaml)` | `<command_root>/artefacts` | `<artifact>/<cdc>` |
| `fpv` | `dirname(fpv.yaml)` | `<command_root>/artefacts` | `<artifact>/<fpv>` |
| `pnr` | `dirname(pnr.yaml)` | `<command_root>/artefacts` | `<artifact>/<pnr>` |
| `power` | `dirname(power.yaml)` | `<command_root>/artefacts` | `<artifact>/<power>` |
| `mut` | `dirname(mut.yaml)` | `<command_root>/artefacts` | `<artifact>/mut/<campaign>` |
| `hier --view dut` | `dirname(models.yaml)` | `<model_root>/artefacts/hier/<model>` | `<artifact>` |
| `hier --view tb` | `dirname(tests.yaml)` | `<suite>/artefacts/hier/<model>/tb/<tb_name>` | `<artifact>` |
| `axi-profile run` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile discover` | `dirname(models.yaml)` | `<model_root>/artefacts/axi/<model>` | `<artifact>` |
| `filelist` | `dirname(models.yaml)` (reads) | explicit `-o` / argument | — |
| `saif` | `invocation_cwd` | explicit output argument | — |
| `hub` | project root | `.rtl-buddy/...` | project root |

For a fuller reference (`docs`, `skill`, edge cases), see the [engineering guidelines](../development/guidelines.md#command-roots) — the table there is the policy this page describes.

## Where `rtl_buddy.log` lives

The orchestration log is always written to `command_root/rtl_buddy.log`. In `--machine` mode it is JSONL; otherwise plain text. For `regression`, each suite's iteration re-anchors the log to that suite's directory, and the final summary phase re-anchors back to `dirname(regression.yaml)`. Open the latest log from wherever the *primary* config lives, not from where you ran `rb`.

## Hook scripts (`sweep`, `preproc`)

The `sweep` and `preproc` hook scripts execute via `exec()` inside the `rb` process and receive `suite_dir` and `artifact_dir` as namespace variables. **Always use these variables.** Do not call `os.getcwd()` inside a hook — the process CWD stays at `invocation_cwd` (the same as your shell), which is no longer the same as `suite_dir`. (The `postproc` hook is parsed from config but the runtime currently relies on built-in post-processing rather than running a user script — see [Plugins](plugins.md).)

```python
# inside a sweep / preproc script
import os
out = os.path.join(artifact_dir, "gen.sv")   # correct
out = os.path.join(os.getcwd(), "gen.sv")    # wrong — invocation cwd
```

## Path resolution rules for config files

`rtl_buddy` resolves config-owned paths from the config file that owns them:

- `regression.yaml` resolves listed suite configs relative to itself.
- `tests.yaml` resolves testbench filelists, hook script paths, and suite-local assets relative to the suite directory.
- `models.yaml` resolves model filelist entries relative to the `models.yaml` file that declared them.
- `synth.yaml`, `cdc.yaml`, `fpv.yaml`, `pnr.yaml`, `power.yaml` resolve their own fields relative to their config directory.

A relative path inside a YAML file never depends on where you ran `rb`. Absolute paths pass through unchanged.

## One run per artefact tree

Two `rb` processes writing into the same `artefacts/` tree would interleave compile workspaces, `run-NNNN` directories, and the latest-run symlinks. To prevent this, every artifact-writing command takes an exclusive advisory lock (`flock`) on `<artifact_root>/.rtl-buddy.lock` when it starts, and **fails immediately** — it never waits — if another run already holds it:

```text
<suite>/artefacts: another rtl-buddy run is already using this artefact tree
(pid 12345, rb test, started 2026-06-10T14:03:21) — wait for it to finish or kill it
```

The lock is released by the kernel when the holding process exits — normal completion, crash, or `kill` alike — so there are no stale locks to clean up; the `.rtl-buddy.lock` file itself is just holder metadata and is harmless to leave behind. `rb regression` locks each suite it enters for the lifetime of the run. Metadata-only invocations (`--list`) never take the lock, so listing tests while a run is in flight always works. Suites with *different* artefact trees never contend — run as many in parallel as you like.

The lock covers the **whole artefact tree**, not just the subtree a command writes: when `tests.yaml`, `cdc.yaml`, `synth.yaml`, etc. share a suite directory, any two artifact-writing commands contend — `rb hier` during a long `rb test` in the same suite fails loud even though their outputs are disjoint. This is deliberate: one coarse lock per tree is simple to reason about and free of partial-overlap edge cases. If you need a read-only view mid-run, the `--list` paths and commands anchored elsewhere (another suite, `rb docs`, `rb hub`) remain available.

**The protection is per host.** The guarantee relies on local `flock(2)` semantics and ends at the machine boundary. On a workspace shared over NFS, two runs on **different hosts** may both acquire "the" lock — whether `flock` propagates across NFS depends on the protocol version, mount options, and server lock-daemon configuration, and rtl_buddy does not rely on it. Concurrent runs against the same suite from two machines are not protected; coordinate those yourself.

## Future: redirecting the artifact root

The artifact root defaults to `command_root/artefacts/`. The `ExecutionContext` carrier is built to accept an explicit override so a future `--artifact-root` flag (or `root_config.yaml` field) can redirect large artifacts — synthesis netlists, simulation waveforms — onto a separate disk without touching command code. None of this is wired today; this page will update when it ships.

## See also

- [Engineering Guidelines — Execution Contexts](../development/guidelines.md#execution-contexts) — the policy form of this page.
- [Regressions](regressions.md) — how the orchestration log re-anchors per suite.
- [Root Config](root-config.md) — how `root_config.yaml` is discovered (walks up from the command root).
