---
description: Define and run tests from tests.yaml, control timeouts and seeds, read verdicts, and reuse compiled builds.
---

# Tests

A `tests.yaml` holds testbench definitions and the runnable tests that use them. `rb test` compiles and simulates the selected tests and reports one verdict per test. This page covers defining and selecting tests, reading verdicts, seeding, and reusing compiled builds.

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

- `plusargs` are runtime arguments. `plusdefines` are compile-time defines.
- `model_path`, testbench filelists and hook paths resolve from the directory containing `tests.yaml`. A model's filelist entries resolve from the directory containing that filelist.
- `toplevel` names the module to elaborate: Verilator `--top-module`, VCS `-top`, Icarus `-s`. Declare it on every testbench, naming the bench and not the DUT. Without it the simulator picks a top from filelist order, and recomposing a filelist can rename the model or raise a `MULTITOP` error. A top pinned in the builder's `compile-time` opts wins; see [Pinning the elaboration top](../reference/yaml.md#pinning-the-elaboration-top).

See [YAML Formats: tests.yaml](../reference/yaml.md#testsyaml) for all fields and [cocotb Testbenches](cocotb.md) for Python-driven tests.

## Run tests

From the suite directory:

```bash
rb test --list
rb test smoke
rb test smoke reset_error timeout
rb test --filter '^smoke_|_error$'
rb test
```

- With no selection, `rb test` runs the whole suite.
- Named tests run in command-line order and produce one table.
- `--filter` is a case-sensitive Python regex search over test names. Matches keep their `tests.yaml` order.
- Names and `--filter` are mutually exclusive. Duplicate or unknown names, an invalid regex, or a regex matching nothing exits 2 before any test runs.
- Selection applies to configured names, before sweep expansion.

From another directory, pass the suite explicitly. Outputs still land beside `tests.yaml` (see [Execution Context](execution-context.md)):

```bash
rb test smoke --test-config path/to/tests.yaml
```

## Override a plusarg for one run

`--plusarg KEY=VALUE` adds or replaces a runtime plusarg for one invocation:

```bash
rb test e2e --plusarg mutate=1 --dispatch slurm
```

- `--plusarg KEY` alone is the valueless `+KEY`. Repeat the flag for several; the last value wins over the YAML and earlier flags.
- The override applies to every selected test, reaches the `preproc` hook (`test_cfg.get_plusarg("mutate")`) and the simulator, and is forwarded to `--dispatch` jobs. It never invalidates a shared build.
- A `sweep` hook does not see the override, and a `preproc` hook that sets the same key wins.
- It cannot be combined with `--list`. Overriding the plusarg named by `sim-rand-seed-plusarg` exits 2; use `--master-seed` or `sim-rand-seed`.

Overrides are recorded as `plusarg_overrides` in `result.json`, the summary footer and `--machine` results.

## Filter by regression level

A test's `reglvl` is one integer or a per-builder mapping:

```yaml
reglvl:
  default: 2500
  vcs: 3500
```

```bash
rb test --reg-level 2000
rb test --start-level 1000 --reg-level 3000
```

The range is inclusive, and tests outside it report `SKIP`. Without either flag `rb test` runs every test. An unqualified [regression](regressions.md#filter-by-regression-level) defaults to level 0.

## Set simulation timeouts

`sim_timeout` is a wall-clock limit in seconds and defaults to 60. For licensed simulators that may queue before running, add a builder-wide allowance to every test's timeout:

```yaml
cfg-rtl-builder:
  - name: vcs
    extra-sim-timeout: 900
```

- `--extra-sim-timeout N` overrides it for one command. 0 disables a configured allowance; negative values are rejected.
- It applies to simulation only, not compilation, and is forwarded to dispatch jobs.
- With VCS `-licqueue`, the timeout pauses while the license-queue banner is printing, for at most one hour.

### Triaging `Sim hit timeout`

`Sim hit timeout` means the wall-clock `sim_timeout` expired. It does not show the test is merely slow. Before raising the limit:

1. Compare sibling tests under the same builder. If they also stall, inspect the shared build, tool or environment.
2. Check whether `test.log` keeps advancing. Steady progress suggests a slow test; repeated activity suggests a functional wedge.
3. Find the last completed phase or transaction and inspect its RTL or testbench condition.
4. Confirm the resolved timeout, including builder and CLI allowances.

A killed simulator may not flush its output, so `test.log` can end mid-line and its last bytes are not the exact stop location.

## Produce a verdict

A plain (non-UVM, non-cocotb) test prints exactly one terminal marker at the start of a line on stdout:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

- `ERR:` or `FAT:` lines after `FAIL` put the reason in the summary.
- If both markers appear, `FAIL` wins and a warning is logged.
- If neither appears, the result is `NA` and the run exits 1. A nonzero simulator exit with no marker is an abort and reports `FAIL`.
- UVM tests are judged by thresholds on the UVM Report Summary; a missing or malformed summary fails the test.
- cocotb tests are judged from `cocotb_results.xml`. Do not print markers.

```yaml
uvm:
  max_warns: 0
  max_errors: 0
```

Setup hooks, filelist validation, compilation and timeouts can also produce `FAIL` before any transcript is parsed.

## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | Simulation completed with a passing transcript, UVM or cocotb verdict |
| `FAIL` | The verdict failed, or setup, filelist, compile or simulation failed |
| `XFAIL`, `XPASS` | Remapped by an [expected-failure](expected-failures.md) marker |
| `SKIP` | Excluded by regression-level or flow filtering |
| `NA` | No verdict: a successful early stop (exits 0) or an unknown outcome (exits 1) |

| Exit code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP` or an early-stop `NA` |
| 1 | A test or tool-flow failure, an unknown `NA`, a strict `XPASS`, or a failed coverage merge |
| 2 | Fatal configuration or environment error |

The exit code is coarse. Under `--machine`, read `payload.results` for per-test verdicts. A setup or compile failure, a sim killed at `sim_timeout` and a job the scheduler lost all exit 1. A requested coverage merge that produced nothing also exits 1 even when every test passed; see [Read a failed merge](coverage.md#read-a-failed-merge).

## Stop after a stage

`-E` / `--early-stop` stops after `pre`, `comp`, `sim` or `post`:

```bash
rb -E comp test smoke
```

A successful early stop reports `NA` with `early_stop: true` in its result row and exits 0. A stage failure still reports `FAIL` and exits 1. An `NA` without that marker is an unknown outcome and exits 1. `NA` is never evidence the DUT passed.

## Sharing compiled builds across tests

`--share-build` reuses one compiled build across tests that differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

`--dispatch` implies it. Verilator, VCS and Icarus support it; with another builder, or an absolute `builder-simv`, the test uses its own build directory and logs why sharing was declined.

- Builds live in `artefacts/.shared-builds/obj_dir_<hash>/`. Plusargs, seeds and simulation timeouts do not affect the build; compile options, plusdefines, the simulator and the resolved filelist do.
- Reuse requires that no tracked input under the project root changed by content. Regenerating a file byte-for-byte reuses the build; any real edit rebuilds it.
- Verilator reports the files it read, so headers, `-y` library files and the Verilator binary itself invalidate the build.
- VCS and Icarus report none, so every file under each `+incdir+` directory (recursively) and `-y` directory (flat) is tracked. Editing, adding or removing any of them rebuilds.
- After a change rtl_buddy cannot see, such as a toolchain change or an include reached through a path no `+incdir+` names, force a compile with `--rebuild`.

### See whether a build was reused

Reuse is announced once per build directory:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild
```

The test's `compile.log` records the same message with the command a rebuild would run. A compile that ran leaves its transcript there. `--rebuild` forces one rebuild per build directory per invocation, and dropping `--share-build` under `--dispatch` does not stop reuse.

### Changed Verilator toolchain

If a Verilator build directory was compiled by a different Verilator (an upgrade, another install, or the same checkout built on a laptop and a cluster node) or runs under `--rebuild`, rtl_buddy deletes its `*.o`, `*.d` and `*.a` files first. This prevents make errors such as `No rule to make target '.../include/verilated.cpp'`. The console reports it:

```text
basic: dropped 12 stale object/dependency files from … (built by …, now …); the C++ build starts clean
```

An ordinary source edit keeps the objects and stays incremental.

## Persistent build cache

`artefacts/.shared-builds/` lives in the workspace, so a CI job that wipes the workspace recompiles unchanged inputs. Set `shared-build-root` to a directory outside the workspace to keep builds across runs:

```yaml
cfg-rtl-reg:
  reg-cfg-path: regression.yaml
  shared-build-root: /shared/nfs/rb-build-cache
```

```bash
rb regression --share-build --shared-build-root /shared/nfs/rb-build-cache
```

- Precedence: `--shared-build-root`, then `RTL_BUDDY_SHARED_BUILD_ROOT`, then the config key. An empty flag or variable turns the cache off for that run.
- A relative root resolves against the project root (the directory holding `root_config.yaml`). The root is created on demand and applies only with `--share-build`.
- Builds land in `<root>/<suite-namespace>/obj_dir_<key>/`, where the namespace is the suite directory relative to the project root with `/` replaced by `__`.
- Every checkout on the host shares the cache. Checkouts with identical inputs reuse each other's build.
- Switching the cache on or off recompiles once.
- Nothing prunes the cache. Prune between runs, never during one (see [Known issues](../known-issues.md)):

```bash
find /shared/nfs/rb-build-cache -mindepth 2 -maxdepth 2 -name 'obj_dir_*' -mtime +14 -exec rm -rf {} +
```

### What keys an entry

The key is built from input contents and project-relative paths, so the same inputs give the same key in any checkout. It covers filelists (expanded recursively), include and library directories, sources, and the values of defines and parameters.

- A generated input that is not byte-reproducible, such as a `preproc` hook stamping a timestamp into a header, changes the key every run and leaves one directory per run behind.
- A path outside the project root is keyed as written, so checkouts that name the same absolute path still share a key.
- A `-f`/`-F` filelist chain nested deeper than the depth bound is only partly read, and the unread entries enter the key as absolute paths. That suite's key becomes checkout-specific, and the console reports `compile.cache_key_depth_bound`.
- An input over 64 MB, such as a ROM image, is keyed by size and modification time. Checkouts with different mtimes stop sharing that suite's builds.

## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed and `--rnd-last` reuses it. `randtest` runs repeated seeded iterations; see the [CLI reference](../reference/cli.md#randtest) for replay and selection options.

To replay a test or regression without old artefacts, pass one master seed:

```bash
rb test smoke --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

- Each runtime seed is derived from the master seed, the `tests.yaml` path relative to the project root, the sweep-expanded test name and the run ID. Selection, ordering, checkout location and dispatch timing do not change it.
- A master seed is a nonnegative integer. Derived seeds, and a fixed `sim-rand-seed`, are 1 through 2147483647.

### Seed a preprocessor

For a `preproc` hook that generates random stimulus, name the plusarg that carries the seed:

```yaml
tests:
  - name: smoke
    sim-rand-seed-plusarg: stimulus_seed
```

- Before `preproc` runs, `test_cfg.get_resolved_seed()` and `test_cfg.get_plusarg("stimulus_seed")` return the resolved seed. The simulator gets that value even if the hook changed the plusarg.
- The seed is written to `test.randseed`, `result.json` and machine results.
- With no master or fixed seed, the plusarg gets the builder's default integer, including `0`.
- `--rnd-new` and `--rnd-last` are rejected for such a test. `randtest` needs a fixed `sim-rand-seed`.

Set `sim-rand-seed` to keep stimulus fixed:

```yaml
tests:
  - name: command_timing
    sim-rand-seed: 41
    sim-rand-seed-plusarg: stimulus_seed
```

A fixed seed overrides the invocation's master, new or replay policy, including for mutation baselines and mutants.

## Inspect artefacts

A run writes to `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories.

- `test.log`, `test.err`: simulator output.
- `test.randseed`: resolved seed.
- `compile.log`: compile output, or the reuse message when nothing compiled.
- `compile.retry.log`: the recompile of a dispatched simulation job whose shared build was stale.
- `run.f`: generated, non-portable filelist.
- `coverage.dat`: raw coverage, when enabled.

Symlinks to the latest test log, error log and seed stay at the test's artefact root. `rtl_buddy.log` beside `tests.yaml` holds orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](../agents.md#machine-mode).

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `rb test` exits 2 before running | Fix the selection: duplicate or unknown name, invalid regex, no `--filter` match, or names combined with `--filter` |
| `Sim hit timeout` | Follow [Triaging `Sim hit timeout`](#triaging-sim-hit-timeout) |
| Result `NA`, exit 1 | The test printed neither `PASS` nor `FAIL`; add a terminal marker |
| Warning that both markers appeared | Print one marker. `FAIL` was used |
| `MULTITOP` error | Declare `toplevel` on the testbench |
| `No rule to make target '.../verilated.cpp'` | Rerun with `--rebuild` to clear objects from another Verilator |
| Stale build reused | Rerun with `--rebuild` after changes rtl_buddy cannot see |
