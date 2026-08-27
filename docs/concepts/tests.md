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

`model_path`, testbench filelists, and hook paths resolve from the directory containing `tests.yaml`. `plusargs` affect simulation; `plusdefines` affect compilation. See [YAML Formats: tests.yaml](../reference/yaml.md#testsyaml) for all fields and [cocotb Testbenches](cocotb.md) for Python-driven tests.

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

Use `ERR:` or `FAT:` after `FAIL` to include the reason in the summary. If both terminal markers appear, `FAIL` wins and RTL Buddy logs a warning. If neither appears, the result is `NA`: it needs review but does not by itself make the shell exit status nonzero. A simulator exit code alone is not a non-UVM verdict.

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
| `NA` | No real verdict was produced, including a successful early stop |

The shell exit code is a coarse run status. Parse `payload.results` under `--machine` for per-test verdicts.

| Code | Meaning |
| --- | --- |
| 0 | No real `FAIL`; may include `PASS`, `XFAIL`, `SKIP`, or `NA` |
| 1 | At least one real test/tool-flow failure, or a strict `XPASS` |
| 2 | Fatal configuration or environment error |

A strict unexpected pass counts as a failure. See [Expected Failures](expected-failures.md).

## Stop after a stage

Use the global `-E` or `--early-stop` option with `pre`, `comp`, `sim`, or `post`:

```bash
rb -E comp test smoke
```

A successful stop before a terminal verdict reports `NA` and exits 0. A stage failure still reports `FAIL` and exits 1. Treat `NA` as requiring inspection, not evidence that the DUT passed.

## Sharing compiled builds across tests

Use `--share-build` when tests differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

RTL Buddy stores shared builds under `artefacts/.shared-builds/obj_dir_<hash>/`. The key includes the resolved simulator executable, compile options, plusdefines, compile environment, and resolved filelist. Plusargs, seeds, and simulation timeouts do not affect it.

A compile stamp records the content hash of every tracked input under the project root, plus toolchain identity. Reuse occurs only while the stamp matches, and content is what decides: regenerating a source byte-for-byte reuses the build; any real edit rebuilds it, including one a node's cached `stat` still describes as the old file. Verilator also reports consumed dependencies, so included headers, `-y` library files, standard includes, and the underlying Verilator binary invalidate the build. VCS and Icarus cannot report equivalent dependencies; after a header-only or hidden toolchain change, force compilation with `--rebuild`.

Reuse is announced rather than inferred from a missing log:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild   # compile it again anyway
```

The test's `compile.log` records the same breadcrumb, with the command a rebuild would run. `--rebuild` forces one rebuild per build directory per invocation and says nothing about whether builds are shared; dropping `--share-build` does not force one, because an unshared build keeps and reuses its own stamp.

Verilator, VCS, and Icarus support shared builds. An unsupported builder or an absolute `builder-simv` uses the test's own build directory and logs why cross-test sharing was declined. RTL Buddy overrides relative output-location options so the shared directory owns `simv`.

## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed; `--rnd-last` reuses it. `randtest` runs repeated seeded iterations. See the [CLI reference](../reference/cli.md#randtest) for replay and selection options.

## Inspect artefacts

Single runs write under `artefacts/<test>/`; repeated runs use `run-NNNN/` subdirectories. Common files are:

- `test.log` and `test.err` — simulator output;
- `test.randseed` — resolved seed;
- `compile.log` — compile output;
- `run.f` — generated non-portable filelist;
- `coverage.dat` — raw coverage when enabled.

Latest-run symlinks for the test log, error log, and seed remain at the test artefact root. `rtl_buddy.log` beside `tests.yaml` contains orchestration events; `--machine` makes it JSON Lines and returns structured stdout. See [Agent Use](../agents.md#machine-mode).
