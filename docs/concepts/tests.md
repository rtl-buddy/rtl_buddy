---
description: Define and run tests from tests.yaml, control timeouts and seeds, interpret verdicts, and reuse compiled builds.
---

# Tests

A verification suite is a `tests.yaml` holding reusable testbench definitions and the runnable tests that use them. This page covers defining, selecting and running tests, reading their verdicts, seeding them, and reusing compiled builds.

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

- `plusargs` are runtime arguments; `plusdefines` are compile-time defines.
- `model_path`, testbench filelists and hook paths resolve from the directory containing `tests.yaml`. A model's filelist entries resolve from the directory containing that filelist, so a `+incdir+` inside a filelist pulled in with `-F` names a directory beside that filelist.
- `toplevel` names the module the compile elaborates from. It becomes Verilator `--top-module`, VCS `-top` or Icarus `-s`. Declare it on every testbench; it is not inferred from the testbench `name`. For a SystemVerilog bench it names the bench, not the DUT.
- Without `toplevel` the simulator picks a top from filelist order. Recomposing a model filelist then renames the Verilator model, and an uninstantiated module in a non-`-v` input becomes a `MULTITOP` error. A top pinned in the builder's `compile-time` opts still wins. See [Pinning the elaboration top](../reference/yaml.md#pinning-the-elaboration-top).

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
- Explicit names run in command-line order and produce one combined results table.
- `--filter` is a case-sensitive Python regex search over configured names. Matches keep their `tests.yaml` order; anchor with `^` or `$` when position matters.
- Names and `--filter` are mutually exclusive. Duplicate or unknown names, an invalid regex, or a regex matching nothing exits 2 before any test runs.
- Selection applies to configured base names, before sweep expansion.

From another directory, pass the suite explicitly. Outputs still land beside `tests.yaml` (see [Execution Context](execution-context.md)):

```bash
rb test smoke --test-config path/to/tests.yaml
```

## Override a plusarg for one run

`--plusarg KEY=VALUE` adds or replaces a runtime plusarg for one invocation without editing `tests.yaml`:

```bash
rb test e2e --plusarg mutate=1 --dispatch slurm
```

- A bare `--plusarg KEY` is the valueless `+KEY`. Repeat the flag for several; the last value wins over both the YAML and earlier flags.
- The override applies to every selected test, reaches the `preproc` hook (`test_cfg.get_plusarg("mutate")`) and the simulator command line, and is forwarded to every `--dispatch` job.
- It cannot be combined with `--list`.
- Plusargs are runtime-only, so an override never invalidates a shared build. There is no compile-time counterpart.
- A `sweep` hook does not see the override, and a `preproc` hook that sets the same key still wins.
- Overriding the plusarg named by a test's `sim-rand-seed-plusarg` exits 2. Choose the seed with `--master-seed` or `sim-rand-seed`.

Each run records its overrides as `plusarg_overrides` in `result.json`, the summary footer and `--machine` results. Typical uses are a deliberate-fault pass proving a bench can fail, a longer `+timeout_us` while debugging, or a `+prog_dir` pointing at a hand-built binary.

## Filter by regression level

A test's `reglvl` is one integer or a builder-specific mapping:

```yaml
reglvl:
  default: 2500
  vcs: 3500
```

```bash
rb test --reg-level 2000
rb test --start-level 1000 --reg-level 3000
```

The range is inclusive, and tests outside it report `SKIP`. `rb test` with neither flag runs every test regardless of level. An unqualified [regression](regressions.md#filter-by-regression-level) defaults to level 0.

## Set simulation timeouts

`sim_timeout` defaults to 60 seconds. For licensed simulators that may wait before running, add a builder-wide allowance to every test's timeout:

```yaml
cfg-rtl-builder:
  - name: vcs
    extra-sim-timeout: 900
```

- `--extra-sim-timeout N` overrides it for one command; 0 disables a configured allowance; negative values are rejected.
- It affects simulation only, not compilation, and is forwarded to local-parallel and Slurm jobs.
- For VCS runs using `-licqueue`, the test timeout pauses while recognized license-queue banner output is active, for at most one hour. It resumes on other simulator output or after the cap.
- The builder allowance still helps with unrecognized or silent license managers.

## Triaging `Sim hit timeout`

`Sim hit timeout` means the wall-clock `sim_timeout` expired. It is not a simulated-time watchdog and does not show the test is merely slow. Before raising the limit:

1. Compare sibling tests under the same builder. If they also stall, inspect the shared build, tool or environment.
2. Check whether timestamps or progress in `test.log` advance. Progress suggests a slow test; repeated activity suggests a functional wedge.
3. Find the last completed phase or transaction and inspect its RTL or testbench condition.
4. Confirm the resolved timeout, including builder and CLI allowances.

A killed simulator may not flush its output, so `test.log` can end mid-line or at a power-of-two byte count. Its final bytes are not the exact stop location.

## Produce a verdict

A non-UVM, non-cocotb test prints exactly one terminal marker at the start of a line on simulator stdout:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

- `ERR:` or `FAT:` after `FAIL` puts the reason in the summary.
- If both markers appear, `FAIL` wins and a warning is logged.
- If neither appears, the result is `NA` and the run exits 1. The simulator exit code alone is not a verdict, except that a nonzero exit with no marker is an abort and reports `FAIL`.
- UVM tests use thresholds; RTL Buddy parses the UVM Report Summary, and a missing or malformed summary fails the test:

```yaml
uvm:
  max_warns: 0
  max_errors: 0
```

- cocotb tests use `cocotb_results.xml`; do not print markers for them.
- Setup hooks, filelist validation, compilation and simulation timeout can also produce `FAIL` before any transcript is parsed.

## Interpret results

| Status | Meaning |
| --- | --- |
| `PASS` | Simulation completed with a passing transcript, UVM or cocotb verdict |
| `FAIL` | The verdict failed, or setup, filelist, compile or simulation failed |
| `XFAIL`, `XPASS` | Remapped by an [expected-failure](expected-failures.md) marker |
| `SKIP` | Excluded by regression-level or flow filtering |
| `NA` | No verdict: a successful early stop (exits 0) or an unknown outcome (exits 1) |

The exit code is a coarse run status. Under `--machine`, read `payload.results` for per-test verdicts.

| Code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP` or an early-stop `NA` |
| 1 | A real test or tool-flow failure, an unknown `NA`, a strict `XPASS`, or a failed coverage merge |
| 2 | Fatal configuration or environment error |

A marker never covers a failure that happened instead of a verdict (setup or compile failure, a sim killed at `sim_timeout`, a job the scheduler lost), so those exit 1. A requested coverage merge that produced nothing also exits 1 even when every test passed; artefacts and results are written first. See [Read a failed merge](coverage.md#read-a-failed-merge).

## Stop after a stage

The global `-E` / `--early-stop` option stops after `pre`, `comp`, `sim` or `post`:

```bash
rb -E comp test smoke
```

A successful stop before a terminal verdict reports `NA`, exits 0, and carries `early_stop: true` in its result row, also under `--machine`. A stage failure still reports `FAIL` and exits 1. An `NA` without that marker is an unknown outcome and exits 1. `NA` needs inspection; it is not evidence the DUT passed.

## Sharing compiled builds across tests

`--share-build` reuses one compiled build across tests that differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

`--dispatch` implies it. Verilator, VCS and Icarus support it. With another builder, or an absolute `builder-simv`, the test uses its own build directory and logs why sharing was declined.

- Builds live in `artefacts/.shared-builds/obj_dir_<hash>/`. The key covers the resolved simulator executable, compile options, plusdefines, compile environment and resolved filelist. Plusargs, seeds and simulation timeouts do not affect it.
- A compile stamp records a content hash of every tracked input under the project root plus the toolchain identity. Reuse needs a matching stamp. Regenerating a file byte-for-byte reuses the build; any real edit rebuilds it.
- Verilator reports the files it consumed, so included headers, `-y` library files, standard includes and the Verilator binary itself invalidate the build.
- VCS and Icarus report none, so the stamp lists each `+incdir+` directory (recursively) and `-y` directory (flat), unfiltered by suffix. Editing, adding or removing any file there rebuilds. For Verilator the listing is compared by name only.
- The listing skips dot-directories, `__pycache__`, `artefacts/`, `.shared-builds/`, `obj_dir*`, editor and VCS bookkeeping files, and RTL Buddy's own outputs by name (`run.f`, `compile.log`, `test.log`, `result.json`, `rtl_buddy.log`, the stamp). A header a `preproc` hook writes into its `artifact_dir` is tracked, as are other dot-files.
- After a change the listing and dependency file cannot see, such as a toolchain change or an include reached through a path no `+incdir+` names, force a compile with `--rebuild`.

## See whether a build was reused

Reuse is announced on the console once per build directory:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild
```

The test's `compile.log` records the same breadcrumb with the command a rebuild would run, and `rtl_buddy.log` records it too. A compile that ran leaves its transcript in `compile.log`.

`--rebuild` forces one rebuild per build directory per invocation. Dropping `--share-build` under `--dispatch` does not stop reuse, because dispatch implies it.

## Recover from a changed Verilator toolchain

A Verilator build directory records the Verilator that compiled it in `rb-toolchain.json`: executable, version and host platform. If the next compile uses a different one (an upgrade, another install, or the same checkout built on a laptop and then a cluster node) or runs under `--rebuild`, RTL Buddy first deletes the directory's `*.o`, `*.d` and `*.a` files. This stops make following dependency files that name the old toolchain's headers (`No rule to make target '.../include/verilated.cpp'`).

The console reports it:

```text
basic: dropped 12 stale object/dependency files from … (built by …, now …); the C++ build starts clean
```

An ordinary source edit keeps the objects and stays incremental.

## Persistent build cache

`artefacts/.shared-builds/` lives in the workspace, so a CI job that wipes the workspace recompiles unchanged inputs. Set `shared-build-root` to a directory outside the workspace to keep the cache across runs:

```yaml
cfg-rtl-reg:
  reg-cfg-path: regression.yaml
  shared-build-root: /shared/nfs/rb-build-cache
```

```bash
rb regression --share-build --shared-build-root /shared/nfs/rb-build-cache
```

- Precedence: `--shared-build-root`, then `RTL_BUDDY_SHARED_BUILD_ROOT`, then the config key. An empty flag or variable turns the cache off for that run, and `--dispatch` forwards that to its build and simulation jobs.
- A relative root resolves against the project root (the directory holding `root_config.yaml`), not the working directory. The root is created on demand and applies only with `--share-build`.
- Builds land in `<root>/<suite-namespace>/obj_dir_<key>/`. The namespace is the suite directory relative to the project root with `/` replaced by `__`; a suite outside the project root gets a digest of its absolute path.
- Every checkout on the host shares the cache. Checkouts with identical inputs reuse each other's build; different inputs get different directories.
- Switching the cache on or off changes every key, so the first run after either compiles once.
- Nothing prunes the cache. Prune between runs, never during one, because the build lock lives inside each directory (see [Known issues](../known-issues.md)):

```bash
find /shared/nfs/rb-build-cache -mindepth 2 -maxdepth 2 -name 'obj_dir_*' -mtime +14 -exec rm -rf {} +
```

A generated input that is not byte-reproducible, such as a `preproc` hook that stamps a timestamp into a header, changes the key each run and strands one directory per run.

## What keys a persistent cache entry

In a persistent cache the key is relative to the project root and includes the content hash of every tracked input, so the same inputs give the same key in any checkout.

- Filelist entries, and in-root paths on the compile line (`+incdir+`, `-y`, `-v`, bare sources, including those from `builder-opts.compile-time`), contribute their content: a file its hash, a directory the hashes of the files inside it, filtered as for the stamp.
- A `-f` / `-F` list contributes every in-root source, include directory and nested list it names, expanded recursively. Relative entries resolve as the simulator resolves them: against the builder's working directory for `-f`, against the list's own directory for `-F`. A chain deeper than the bound fails closed: unread inputs enter the key as absolute paths, making that suite's key checkout-specific, and `compile.cache_key_depth_bound` is logged once.
- A path embedded in another option, such as `-CFLAGS=-I<root>/inc`, is relativised and keyed by its directory listing. A relative spelling such as `-CFLAGS=-I../../inc` is recognised after `-I`, `+incdir+` or `-y`.
- `+define+NAME=<path>`, `-D`, `-G`, `-pvalue+`, `+libext+` and other `key=value` tokens keep their value verbatim, because the value compiles into the model.
- Paths outside the project root stay in the key as text, except that a relative path is resolved against the builder's working directory and keyed by contents. Output options such as `-o` are relativised but not read.
- An input over 64 MB, typically a ROM or memory image, is keyed by size and modification time. Checkouts with different mtimes then stop sharing that suite's builds; each still reuses its own.

## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed and `--rnd-last` reuses it. `randtest` runs repeated seeded iterations; see the [CLI reference](../reference/cli.md#randtest) for replay and selection options.

To replay a test or regression without relying on old artefacts, pass one master seed:

```bash
rb test smoke --master-seed 20260914
rb regression --master-seed 20260914 --dispatch slurm
```

- Each runtime seed is derived from the master seed, the project-root-relative `tests.yaml` path, the sweep-expanded test name and the run ID when present.
- Test selection, ordering, checkout location and dispatch timing do not change it, so the same command replays the same seeds.
- A master seed is a nonnegative integer and may exceed the simulator's range. Master-derived seeds are 1 through 2147483647, and so is the allowed range for a fixed `sim-rand-seed`.

## Seed a preprocessor

For a preprocessor that generates random stimulus, name the plusarg that carries the seed:

```yaml
tests:
  - name: smoke
    sim-rand-seed-plusarg: stimulus_seed
```

- Before `preproc` runs, `test_cfg.get_resolved_seed()` and `test_cfg.get_plusarg("stimulus_seed")` return the resolved seed. The simulator receives that value even if the hook changed the plusarg.
- The seed is written to `test.randseed`, `result.json`, structured logs and machine results. Seeds and plusargs do not change the compile key.
- Without a master or fixed seed, the plusarg gets the builder's default integer unchanged, including `0`. That default is exempt from the 1 to 2147483647 range.
- `--rnd-new` and `--rnd-last` are rejected for such a test, because they choose their value too late for preprocessing. `randtest` needs a fixed `sim-rand-seed`; its single preprocessor run and every iteration use that value.

Set `sim-rand-seed` to keep timing or command-cycle stimulus fixed:

```yaml
tests:
  - name: command_timing
    sim-rand-seed: 41
    sim-rand-seed-plusarg: stimulus_seed
```

A fixed seed overrides the invocation's master, new or replay policy. Mutation simulation oracles also resolve fixed seeds and exposed builder defaults before preprocessing, for the baseline and every mutant.

## Inspect artefacts

A single run writes to `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories. Common files:

- `test.log`, `test.err`: simulator output.
- `test.randseed`: resolved seed.
- `compile.log`: compile output, or the reuse breadcrumb when nothing compiled.
- `compile.retry.log`: the recompile of a dispatched simulation job whose build job's stamp did not validate. It lives in that run's own directory and never replaces the build job's `compile.log`.
- `run.f`: generated, non-portable filelist.
- `coverage.dat`: raw coverage, when enabled.

Symlinks to the latest test log, error log and seed stay at the test's artefact root. `rtl_buddy.log` beside `tests.yaml` holds orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](../agents.md#machine-mode).
