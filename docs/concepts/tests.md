---
description: Define and run tests from tests.yaml, control timeouts and seeds, interpret verdicts, and reuse compiled builds.
---

# Tests

Each verification suite has a `tests.yaml` containing reusable testbench definitions and runnable tests.

## Define a suite

```yaml
rtl-buddy-filetype: test_config

testbenches:
  - name: tb_top
    toplevel: tb_top
    filelist:
      - +incdir+../../../verif/tb
      - tb_top.sv

tests:
  - name: smoke
    desc: sanity test
    reglvl: 0
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    plusargs:
      test_cycles: 50
    plusdefines:
      FEATURE_X: 1
    sim_timeout: 120
```

`model_path`, testbench filelists, and hook paths resolve from the directory containing `tests.yaml`, and a model's filelist entries resolve from the directory containing its own filelist — a `+incdir+` inside a filelist pulled in with `-F` names a directory beside that filelist, not beside the suite that consumes it, so a design filelist can carry its own include path. `plusargs` affect simulation; `plusdefines` affect compilation. See [YAML Formats: tests.yaml](../reference/yaml.md#testsyaml) for all fields and [cocotb Testbenches](cocotb.md) for Python-driven tests.

`toplevel:` names the module the compile elaborates from and reaches the builder as Verilator `--top-module`, VCS `-top`, or Icarus `-s`. Declare it on every testbench: without it the simulator elects a top from filelist order, so recomposing a model filelist renames the Verilator model and an uninstantiated module in an ordinary (non-`-v`) input turns the build into a `MULTITOP` error. For a SystemVerilog bench it names the bench, not the DUT. It is not inferred from `name`, and a top pinned in the builder's `compile-time` opts still wins. See [Pinning the elaboration top](../reference/yaml.md#pinning-the-elaboration-top).

## Run tests

From the suite directory:

```bash
rb test --list
rb test smoke
rb test smoke reset_error timeout
rb test --filter '^smoke_|_error$'
rb test
```

With no selection, `rb test` runs the suite. Explicit names run in command-line order and produce one combined results table. `--filter` uses a case-sensitive Python regex search against configured names; matches retain their `tests.yaml` order. Anchor the expression with `^` or `$` when position matters.

Explicit names and `--filter` are mutually exclusive. Duplicate or unknown names, an invalid regex, or a regex with no matches exits 2 before any test runs. Selection applies to configured base names before sweep expansion.

From another directory:

```bash
rb test smoke --test-config path/to/tests.yaml
```

Outputs remain beside `tests.yaml`; see [Execution Context](execution-context.md).

## Filter by regression level

A test's `reglvl` may be one integer or a builder-specific mapping:

```yaml
reglvl:
  default: 2500
  vcs: 3500
```

Filter a single suite with:

```bash
rb test --reg-level 2000
rb test --start-level 1000 --reg-level 3000
```

The range is inclusive. Tests outside it report `SKIP`. With `rb test`, omitting both flags runs every test regardless of level. An unqualified [regression](regressions.md#filter-by-regression-level) instead defaults to level 0.

## Set simulation timeouts

`sim_timeout` defaults to 60 seconds. Add a builder-wide allowance for licensed simulators that may wait before running:

```yaml
cfg-rtl-builder:
  - name: vcs
    extra-sim-timeout: 900
```

The allowance is added to each test's timeout. Override it for one command with `--extra-sim-timeout N`; use 0 to disable a configured allowance. Negative values are rejected. The setting affects simulation only, not compilation, and is forwarded to local-parallel and Slurm jobs.

For VCS runs using `-licqueue`, RTL Buddy pauses the test timeout while recognized license-queue banner output is active, for at most one hour. The timer resumes on other simulation output or after the cap. This avoids false timeouts without allowing an indefinite queue wait. Builder allowance remains useful for unrecognized or silent license managers.

## Triaging `Sim hit timeout`

`Sim hit timeout` means the wall-clock limit expired; it does not identify a simulated-time watchdog or prove the test is merely slow. Before raising the limit:

1. Compare sibling tests under the same builder. If they also stall, inspect the shared build, tool, or environment.
2. Check whether timestamps or progress in `test.log` advance. Progress suggests a slow test; repeated activity suggests a functional wedge.
3. Identify the last completed phase or transaction and inspect its RTL or testbench condition.
4. Confirm the resolved timeout, including builder and CLI allowances.

A killed simulator may not flush its output, so `test.log` can end mid-line or at a power-of-two byte count. Do not treat its final bytes as the exact stop location.

## Produce a verdict

For a non-UVM, non-cocotb test, print exactly one terminal marker to simulator stdout at the start of a line:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

Use `ERR:` or `FAT:` after `FAIL` to include the reason in the summary. If both terminal markers appear, `FAIL` wins and RTL Buddy logs a warning. If neither appears, the outcome is unknown: the result is `NA` and the run exits 1. A simulator exit code alone is not a non-UVM verdict, but a simulator that exits nonzero *and* prints no marker has aborted, and that combination is reported as `FAIL`.

For UVM, configure thresholds and let RTL Buddy parse the UVM Report Summary:

```yaml
uvm:
  max_warns: 0
  max_errors: 0
```

A missing or malformed UVM summary fails the test. cocotb tests use `cocotb_results.xml` instead; do not print transcript markers for them.

Setup hooks, filelist validation, compilation, and simulation timeout can also produce `FAIL` before transcript parsing.

## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | Simulation completed with a passing transcript, UVM, or cocotb verdict |
| `FAIL` | The verdict failed, or setup, filelist, compile, or simulation failed |
| `XFAIL`, `XPASS` | Remapped by an expected-failure marker |
| `SKIP` | Excluded by regression-level or flow filtering |
| `NA` | No verdict was produced: either a successful early stop (exits 0) or an unknown outcome (exits 1) |

The shell exit code is a coarse run status. Parse `payload.results` under `--machine` for per-test verdicts.

| Code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP`, or an early-stop `NA` |
| 1 | At least one real test/tool-flow failure, an unknown `NA`, or a strict `XPASS` |
| 2 | Fatal configuration or environment error |

A strict unexpected pass counts as a failure. See [Expected Failures](expected-failures.md).

## Stop after a stage

Use the global `-E` or `--early-stop` option with `pre`, `comp`, `sim`, or `post`:

```bash
rb -E comp test smoke
```

A successful stop before a terminal verdict reports `NA` and exits 0; its result row carries `early_stop: true` (in `--machine` output too) so tooling can tell it apart. A stage failure still reports `FAIL` and exits 1. An `NA` that was not asked for — no verdict in the transcript — is an unknown outcome, carries no marker, and exits 1. Treat `NA` as requiring inspection, not evidence that the DUT passed.

## Sharing compiled builds across tests

Use `--share-build` when tests differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

RTL Buddy stores shared builds under `artefacts/.shared-builds/obj_dir_<hash>/`. The key includes the resolved simulator executable, compile options, plusdefines, compile environment, and resolved filelist. Plusargs, seeds, and simulation timeouts do not affect it.

A compile stamp records the content hash of every tracked input under the project root, plus toolchain identity. Reuse occurs only while the stamp matches, and content is what decides: regenerating a source byte-for-byte reuses the build; any real edit rebuilds it, including one a node's cached `stat` still describes as the old file. Verilator also reports consumed dependencies, so included headers, `-y` library files, standard includes, and the underlying Verilator binary invalidate the build. VCS and Icarus report none, so the stamp additionally lists each `+incdir+` and `-y` directory the filelist names: without a dependency file, editing, adding, or removing a file in one rebuilds; with one, the listing is compared by name alone, because the dependency file already decides the content of everything the build read and an added `-y` file is the case it cannot report. Listings are unfiltered by suffix; an `+incdir+` is walked recursively and a `-y` directory listed flat, following what each option's search can reach. The walk skips dot-directories, `__pycache__`, and RTL Buddy's own `artefacts/`, `.shared-builds/` and `obj_dir*` trees, plus editor and VCS bookkeeping files and RTL Buddy's own outputs by name (`run.f`, `compile.log`, `test.log`, `result.json`, `rtl_buddy.log`, the stamp, and the rest) — all of those are written after the fingerprint that would list them, so stamping one would make every later run recompile. A header a `preproc` hook generates into its `artifact_dir` **is** tracked, and other dot-files are too, since `` `include ".config.svh" `` resolves. After a change outside what the listing and a dependency file cover — a hidden toolchain change, an include reached by a path no `+incdir+` names — force compilation with `--rebuild`.

Reuse is announced rather than inferred from a missing log:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild   # compile it again anyway
```

The test's `compile.log` records the same breadcrumb, with the command a rebuild would run. `--rebuild` forces one rebuild per build directory per invocation and says nothing about whether builds are shared; dropping `--share-build` does not force one under `--dispatch`, which implies it.

Verilator, VCS, and Icarus support shared builds. An unsupported builder or an absolute `builder-simv` uses the test's own build directory and logs why cross-test sharing was declined. RTL Buddy overrides relative output-location options so the shared directory owns `simv`.

### Persistent build cache

`artefacts/.shared-builds/` lives inside the workspace, so a CI job that wipes the workspace after every run (`deleteDir()`, `git clean -ffdx`) throws the cache away and recompiles inputs that never changed. Point `shared-build-root` at a directory outside the workspace and the cache outlives the run:

```yaml
cfg-rtl-reg:
  reg-cfg-path: regression.yaml
  shared-build-root: /shared/nfs/rb-build-cache
```

```bash
rb regression --share-build --shared-build-root /shared/nfs/rb-build-cache
```

`--shared-build-root` beats `RTL_BUDDY_SHARED_BUILD_ROOT`, which beats the config key; an empty value at either of the first two turns the cache off for that run. A relative root resolves against the project root — the directory holding `root_config.yaml` — not the working directory, so a dispatched build job and its simulation jobs resolve one path. The root is created on demand and only applies with `--share-build` (which `--dispatch` implies).

Builds land in `<root>/<suite-namespace>/obj_dir_<key>/`, where the namespace is the suite directory relative to the project root with `/` replaced by `__` (`verif/demo_tiny_alu` → `verif__demo_tiny_alu`); a suite outside the project root gets a digest of its absolute path instead. Nothing in the layout names the checkout, which is the point: every checkout on the host shares the cache.

In this mode the compile key changes shape. It is spelled relative to the project root — `run.f` entries and any absolute in-root path in the compile line alike — and it **includes the content hash** of every tracked input. That covers what the compile *line* names as well as what `run.f` does: an absolute in-root `+incdir+` or `-y` directory, a `-v` file, or a bare source path reaching the builder through `builder-opts.compile-time` contributes the same content identity a filelist entry does (a file its hash, a directory the hashes of the files inside it, pruned and filtered by the same rules). A `-f`/`-F` filelist contributes what it *names*, not just its own bytes: the chain is expanded recursively (bounded depth, cycle-safe) and every in-root source, include directory and further list in it is keyed. The two options are read the way the simulators read them — a relative entry inside a `-f` list resolves against the builder's working directory, one inside a `-F` list against the directory holding that list, and a nested `-f`/`-F` resets the rule for the file it names — so the key hashes the files the build actually opens. A relative entry whose base is unknown is left as text rather than guessed at. Otherwise two checkouts whose `run.f` matched but whose header under such an `+incdir+` — or whose RTL behind byte-identical nested lists — differed would take one directory and rebuild over each other. (`run.f` itself has no nested lists: RTL Buddy unrolls every `-F` chain when it writes one.)

Only tokens RTL Buddy resolves as a path are relativised. A `+define+NAME=<path>` — on the compile line or in `tests.yaml` `plusdefines`, which reach `run.f` — a `-D`/`-G`/`-pvalue+`, a `+libext+`, or any other `key=value` token keeps its value exactly as written, because a define's value is compiled *into* the model: two checkouts whose builds bake in different absolute paths must get different keys, not one shared binary. An in-root path *embedded* in a larger option, such as `-CFLAGS=-I<root>/inc`, is the other way round — it names a directory the build really reads, so it is relativised **and** keyed by its listing. A compile-line path *outside* the project root, or one spelled relative (which the builder resolves against its own working directory, not RTL Buddy), stays part of the key as text; an output location such as `-o` is relativised so the key carries no checkout prefix, but is never read. Only `artefacts/`, `.shared-builds/` and `obj_dir*` *below the project root* are treated as build output, so a workspace that itself sits under a dot-directory (`/home/ci/.worktrees/pr`) is keyed like any other.

An input RTL Buddy cannot hash — one above the 64 MB cap, typically a ROM or memory-init image — falls back to its size and modification time rather than to nothing, so two checkouts with different images cannot collide on one directory. Mtimes differ per checkout, so a suite with such an input stops sharing builds across checkouts; it still reuses its own. Inputs outside the project root are unaffected: two checkouts naming one absolute path name the same bytes.

Two checkouts with byte-identical inputs therefore get the same directory and reuse each other's build wherever they sit; two checkouts on different commits get different directories, instead of rebuilding over one another. That makes the directory content-addressed, so a run keeps one directory per distinct input set rather than one per key — a cache to prune rather than a build to rebuild. The stamp is checked on top of the key as usual, and records the project root it was written from so another checkout can re-anchor its entries.

Switching the cache on or off changes every key, so the first run after either compiles once. Nothing prunes the cache; do it between runs, never during one (the build lock lives inside the directory it guards — see [Known issues](../known-issues.md)):

```bash
find /shared/nfs/rb-build-cache -mindepth 2 -maxdepth 2 -name 'obj_dir_*' -mtime +14 -exec rm -rf {} +
```

A generated input that is not reproducible byte-for-byte — a `preproc` hook that stamps a timestamp into a header — moves the key rather than only the stamp here, so it strands a directory per run instead of rebuilding in place.

## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed; `--rnd-last` reuses it. `randtest` runs repeated seeded iterations. See the [CLI reference](../reference/cli.md#randtest) for replay and selection options.

Use one explicit master seed when a test or regression must replay without
depending on old artefacts:

```bash
rb test smoke --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

RTL Buddy derives each runtime seed from the master seed, the
project-root-relative `tests.yaml` path, the sweep-expanded test name, and the
run ID when present. Test selection, ordering, checkout location, and dispatch
timing do not change it. Repeating the command with the same master seed
replays the same seeds. Master seeds are nonnegative integers and may exceed
the simulator's seed range; derived simulator seeds are from 1 through
2147483647.

Configure `sim-rand-seed-plusarg` when a preprocessor generates randomized
stimulus:

```yaml
tests:
  - name: smoke
    # ...
    sim-rand-seed-plusarg: stimulus_seed
```

The resolved value is available through
`test_cfg.get_resolved_seed()` and
`test_cfg.get_plusarg("stimulus_seed")` before `preproc` runs. RTL Buddy then
passes that value to the simulator, restores the configured plusarg if the
hook changed it, writes it to `test.randseed` and `result.json`, and includes
it in structured logs and machine results. Runtime seed values and plusargs do
not change the compile key.

Without a master or fixed test seed, the plusarg receives the builder's default
integer unchanged, including `0`; the positive 31-bit limit applies only to
fixed test seeds and master-derived seeds. `--rnd-new` and `--rnd-last` are
rejected for a test that configures this plusarg because those modes select their value too late for preprocessing.
`randtest` therefore requires a fixed `sim-rand-seed` for such a test; its one
shared preprocessor run and every iteration receive that fixed value.

Set `sim-rand-seed` on a test whose timing or command-cycle stimulus must stay
fixed:

```yaml
tests:
  - name: command_timing
    # ...
    sim-rand-seed: 41
    sim-rand-seed-plusarg: stimulus_seed
```

The fixed value overrides the invocation's master, new, or replay seed policy.
Mutation simulation oracles also resolve fixed seeds and exposed builder defaults
before preprocessing, for both the baseline and every mutant.
See [YAML Formats: tests.yaml](../reference/yaml.md#testsyaml) for the field
contract.

## Inspect artefacts

Single runs write under `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories. Common files are:

- `test.log` and `test.err` — simulator output;
- `test.randseed` — resolved seed;
- `compile.log` — compile output;
- `compile.retry.log` — output of a dispatched simulation job's recompile, written only when that job was gated on a build job whose stamp did not validate. Run-scoped: it lives in the run's own directory (`run-NNNN/` for a fanned-out test), because only the run that retried wrote it. It never replaces `compile.log`, so a build job's own compile record survives its simulation jobs' recompiles;
- `run.f` — generated non-portable filelist;
- `coverage.dat` — raw coverage when enabled.

Latest-run symlinks for the test log, error log, and seed remain at the test artefact root. `rtl_buddy.log` beside `tests.yaml` contains orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](../agents.md#machine-mode).
