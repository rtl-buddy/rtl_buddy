---
description: How to define testbenches and tests in tests.yaml for a verification suite.
---

# Tests

## Test config: `tests.yaml`

A `tests.yaml` file defines the testbenches and tests for a verification suite. Each suite has its own `tests.yaml`.

`rtl_buddy` looks for `tests.yaml` in the current directory, or you can specify a file with `--test-config`.

### Structure

```yaml
rtl-buddy-filetype: test_config

testbenches:
  - name: "tb_top"
    filelist:
      - "+incdir+../../../verif/tb"
      - "tb_top.sv"

tests:
  - name: "smoke"
    desc: "sanity test"
    reglvl: 0
    model: "my_design"
    model_path: "../src/models.yaml"
    testbench: "tb_top"
    plusargs:
      test_cycles: "50"
    plusdefines:
      FEATURE_X: "1"
    sim_timeout: 120
```

### Test fields

| Field | Description |
|-------|-------------|
| `name` | Test identifier used on the command line and in log file names |
| `desc` | Human-readable description |
| `reglvl` | Regression level (int or per-builder dict) |
| `model` | Model name from `models.yaml` |
| `model_path` | Path to `models.yaml`, resolved relative to the suite directory |
| `testbench` | Testbench name from `testbenches` list |
| `plusargs` | Key-value pairs passed as `+KEY=VALUE` at sim runtime |
| `plusdefines` | Key-value pairs passed as `+define+KEY=VALUE` at compile time |
| `sim_timeout` | Timeout in seconds (default: 60) |
| `uvm` | UVM report thresholds (see below) |
| `sweep` | Sweep expansion script (see [Plugins](plugins.md)) |
| `preproc` | Pre-processing script (see [Plugins](plugins.md)) |
| `assertions` | Boolean: compile in SVA (`--assert`) and report firings (see [Assertion-Based Verification](abv-simulation.md)) |

### VCS license-queue waits and `sim_timeout`

When a VCS `simv` run is invoked with `-licqueue` and no seat is free, it prints a `Queuing for License` banner and blocks until one opens up. `rtl_buddy` detects that banner (and the `Licensed number of users already reached` variant) in the sim's output and pauses the `sim_timeout` clock for as long as the sim is queuing, so a busy license server doesn't cause a false timeout. There is a 1-hour safety cap on total queued time — if the sim is still queuing after that, `sim_timeout` resumes counting and the sim can time out normally.

The pause ends on the first line that is not part of the banner. The banner's vocabulary is the two markers, blank lines, the queue-polling dots, and the `HIT CTRL-C to exit` hint; anything else counts as simulation output. A banner line VCS adds in a future release would therefore end the pause early until it is recognised, and the safety cap is what bounds that.

### Extra simulation timeout per builder

Detecting the banner cannot cover every case: the text is a vendor string, and other license managers queue without printing anything recognisable. `extra-sim-timeout` on a builder in `root_config.yaml` adds seconds to every test's `sim_timeout` under that builder:

```yaml
cfg-rtl-builder:
  - name: "vcs"
    extra-sim-timeout: 900
```

It is added to the test's own `sim_timeout` rather than replacing it, so per-test values keep their meaning. Keeping it per-builder is the point: a licensed simulator can wait, while a builder that never blocks keeps a tight timeout so a genuinely hung test still fails fast. `--extra-sim-timeout N` overrides every builder for one run, and `--extra-sim-timeout 0` turns a configured allowance off. When either applies, the resolved value is logged as `sim.timeout_extended`. A negative value is rejected: on the CLI by `min=0`, and in `root_config.yaml` with a fatal config error, because it would shrink the timeout rather than extend it.

This covers the simulation phase only. `compile()` sets no timeout, so a license-queue wait during elaboration is bounded by the dispatch job's own time limit rather than by anything here; `has_license_queue_marker` attributes such a compile after the fact so a slow build is not mistaken for an undersized reservation.

Under `--dispatch slurm` or `--dispatch local-parallel`, both paths reach the dispatched job: a builder's `extra-sim-timeout` because the child re-reads `root_config.yaml`, and `--extra-sim-timeout` because it is forwarded in the job's argv.

### Triaging `Sim hit timeout`

`Sim hit timeout` means rtl_buddy's wall-clock timer reached the resolved
`sim_timeout` and killed the simulator process. It says nothing about a
testbench's simulated-time watchdog: a design can stop advancing simulated time
while the wall clock keeps running, or a healthy but slow simulation can advance
until the harness limit expires.

Before increasing the limit:

1. Run or inspect sibling tests under the same builder. If they also stall, start
   with the shared build, tool, or environment rather than one test's timeout.
2. Check `test.log` for timestamp or progress changes. Advancing simulated time
   suggests a slow test; repeated last activity suggests a functional wedge.
3. Identify the last completed phase or transaction, then check the corresponding
   RTL/testbench condition before extending the wall-clock budget.
4. Confirm the resolved value. A test without `sim_timeout` uses the 60-second
   default, plus any builder/CLI allowance described above.

Do not treat the final bytes in `test.log` as an exact stop location. A process
killed at timeout may not flush its userspace output buffer, so the captured log
can end mid-line—often at a power-of-two size—and omit the simulator's real tail.
See [Known Issues](../known-issues.md#a-timeout-kill-can-leave-testlog-with-an-unflushed-tail).

### Regression levels

`reglvl` controls which tests run during a regression:

```yaml
# Same level for all builders
reglvl: 1500

# Builder-specific, with a fallback
reglvl:
  default: 2500
  vcs: 3500
```

Use `--reg-level` and `--start-level` on the `regression` subcommand to select a level range. See [Regressions](regressions.md).

The `test` subcommand accepts the same two long-form options (no short flags), so a single `tests.yaml` suite can be filtered by regression level without a `regressions.yaml`:

```bash
# Run only tests with reglvl <= 2000
rtl-buddy test --reg-level 2000

# Run tests with reglvl in [1000, 3000]
rtl-buddy test --start-level 1000 --reg-level 3000
```

Tests with `reglvl` above `--reg-level` or below `--start-level` are reported as `SKIP`. Unlike `regression`, omitting both flags on `test` runs every test regardless of `reglvl` — filtering only kicks in once one of the flags is given.

### Default transcript parsing

When `uvm` is **not** set, `rtl_buddy` determines the result by parsing `artefacts/{test_name}/test.log` after simulation. Your testbench must print a result marker to **stdout** at the start of a line:

- `PASS <optional detail>`
- `FAIL <optional detail>`

When emitting `FAIL`, also print an `ERR:` or `FAT:` line. It is optional, but its text is appended to the failing test's description in the results table, so it is what makes a red row explain itself:

```systemverilog
if (test_passed) begin
  $display("PASS smoke completed");
end else begin
  $display("FAIL smoke completed");
  $display("ERR: expected done=1 before timeout");
end
```

Rules to follow:

- Emit exactly one terminal result marker.
- Start the line with `PASS` or `FAIL`; other wording will not be detected.
- Write the marker to stdout, not stderr.
- When using `FAIL`, follow it with an `ERR:` or `FAT:` line so the results table can say why the test failed.
- If a log carries **both** markers, `FAIL` wins, whichever came first, and a `postproc.conflicting_markers` warning is logged. A failure signal is never erased by a `PASS` line elsewhere in the transcript.
- If no `PASS` or `FAIL` marker is found, `rtl_buddy` records the test as `NA` with description `test result unknown`. `NA` is non-passing and needs review, but does not by itself make the shell exit status nonzero.
- Do not rely on the simulator exit code alone to communicate pass/fail in non-UVM tests.

### UVM report parsing

When `uvm` is set, `rtl_buddy` parses the UVM summary at the end of simulation output and fails the test if thresholds are exceeded:

```yaml
uvm:
  max_warns: 0
  max_errors: 0
```

With `uvm` enabled, `rtl_buddy` uses the UVM Report Summary instead of `PASS` / `FAIL` transcript markers. Missing or malformed UVM summaries are treated as test failures.

### Other failure modes

The transcript parser is not the only source of failures. `rtl_buddy` also marks a test as `FAIL` when:

- a sweep or pre-processing script fails during setup
- filelist validation fails before compile
- compilation fails
- simulation times out

### Stopping early

The global `-E`/`--early-stop` option halts a run after a given stage (`pre`, `comp`, `sim`, or `post`):

```bash
rtl-buddy -E comp test smoke
```

Stopping after a successful stage (e.g. `comp`) reports result `NA` (e.g. desc "Stopped early at compile") rather than a `PASS`/`FAIL` transcript verdict, since simulation never ran to produce one — an early stop is an intentional non-verdict, not evidence the DUT passed, so it needs hand-checking like any other `NA`. A successful early stop exits 0 because it produced no `FAIL`. If compilation itself fails, the result is `FAIL` with exit code 1, regardless of `--early-stop`.

### Result statuses

`rtl_buddy` reports one of these result statuses per test:

| Result | Meaning |
|--------|---------|
| `PASS` | A real simulation run completed and the transcript/UVM verdict was a pass |
| `FAIL` | A real simulation run failed, or a tool/flow step failed (setup, filelist, compile, sim timeout) |
| `XFAIL` / `XPASS` | `PASS`/`FAIL` remapped by `xfail`/`xfail_strict` — see [Expected failures](expected-failures.md) |
| `SKIP` | A regression-level skip (`reglvl` outside `--reg-level`/`--start-level`) |
| `NA` | Everything else, including all intentional early stops — no real pass/fail verdict was produced, so the result needs hand-checking |

### Exit codes

The shell exit code is a coarse run status: it distinguishes a result set with a
real `FAIL` from one without, and separates fatal configuration/environment
errors. It does not preserve the per-test verdicts or distinguish `PASS`, `SKIP`,
`XFAIL`, and `NA`; under `--machine`, parse `payload.results` for those details.

| Code | Meaning |
|------|---------|
| 0 | No real `FAIL` verdicts — includes `PASS`, `SKIP`, `XFAIL`, and `NA` (early stops included) |
| 1 | One or more tests resulted in a real `FAIL`, or strict expected-failure handling produced `XPASS` |
| 2 | Fatal configuration or environment error |

## Running tests

Run a named test:
```bash
rtl-buddy test smoke
```

Run all tests in a config:
```bash
rtl-buddy test
```

List tests without running:
```bash
rtl-buddy test --list
```

### Sharing compiled builds across tests

By default every test compiles into its own build directory
(`artefacts/<test>/obj_dir_<test>`), so a suite of N tests that share one
testbench verilates the design N times. For large designs the verilation
step dominates wall-clock time.

`--share-build` opts into reusing one compiled `simv` across tests whose
compile inputs are identical:

```bash
rtl-buddy test --share-build
rtl-buddy regression --share-build
```

The build directory is keyed on a hash of the compile inputs — the builder
executable *as `PATH` resolves it*, compile-time options, plusdefines,
compile environment, and the resolved filelist — and lives at
`artefacts/.shared-builds/obj_dir_<hash>/`. Resolving the executable is what
keeps two simulator installs to two build dirs, so pointing the project at a
different one compiles rather than reusing what the other built, and an A/B
between them keeps both builds runnable.
The first test with a given key compiles; subsequent tests find a valid
`simv` and skip verilation entirely. Runtime-only inputs (plusargs, seeds,
`timeout`) never affect the key, so tests that differ only in those always
share. Tests with different `pd` plusdefines hash to different keys and
compile separately.

After a successful compile, a `rb-compile-stamp.json` recording the exact
compile inputs (including each source file's size and modification time) is
written next to the `simv`. Reuse only happens when the stamp matches, so
editing any file listed in the filelist triggers a rebuild in place.

The stamp also names the toolchain that produced the build: the resolved
executable, its size and mtime, and — for Verilator and Icarus — the first
line of its version banner, probed once per binary per process. Upgrading one
install in place therefore rebuilds *in the same directory* (the path did not
change, so the key did not either) and logs
`compile.build_toolchain_changed` naming the old and new versions, while
`compile.build_reused` names the toolchain whose output a skipped compile is
about to simulate. The version is the entry that catches an upgrade a wrapper
script hides: `bin/verilator` can keep its own size and mtime across a
rebuild of the `verilator_bin` it dispatches to. VCS is stamped by path, size
and mtime only — `vcs -ID` checks out a licence, and queueing for one before
every compile would cost more than the check is worth.

The filelist only names what the *project* declared, which is not the whole
truth: an entry that resolves to a directory (`+incdir+`, `-y`) says nothing
about the headers reached through it. So the stamp also records every input
the builder reports having actually consumed. **Under Verilator** that comes
from the `V<prefix>__ver.d` dependency file it emits into the build dir, and
it covers included headers, files pulled in from `-y` library dirs,
Verilator's own std includes, and the `verilator_bin` binary itself — so a
header-only edit *and* an in-place toolchain upgrade both invalidate the
stamp. Other builders emit nothing comparable; their stamps record that no
dependency information was available (`"deps": null`) and reuse behaves as
it did before. `compile.build_stamp_written` logs how many inputs were
tracked, and `compile.build_dep_changed` names the one that forced a
rebuild.

Where the build lands depends on the builder, but the executable is always
`simv` inside the shared dir: Verilator is pointed there with `--Mdir`, VCS
with `-o` (plus `-Mdir` so its `csrc` tree stays beside the binary), and
Icarus with `-o` for the `.vvp` snapshot the wrapper `simv` execs. The shared
build owns the output location, so a `-o` / `-Mdir` in `builder-opts` and a
*relative* `builder-simv:` are both dropped in favour of `<shared>/simv`
(logged at DEBUG as `compile.share_build_opts_overridden` /
`compile.share_build_simv_overridden`). An **absolute** `builder-simv:`
instead declines sharing altogether — see the caveats below.

Caveats:

- Verilator, VCS, and Icarus builders share one build. Others log a warning
  (`compile.share_build_unsupported`, with the reason) and keep the build in
  the test's own artefact dir, as does any builder whose `builder-simv:` is
  an absolute path — that pins the executable outside the shared dir. Such a
  build is still stamped where it lands, so *that test* reuses it on its
  next run; only cross-test sharing is lost. (That reuse is what lets a
  dispatched fan-out compile once in the build job instead of racing N
  compiles into one directory — see
  [Builders that compile inside the job](dispatch.md#builders-that-compile-inside-the-job).)
- Include-dir headers and toolchain changes are tracked **only where the
  builder reports its inputs** — Verilator today. Under VCS or Icarus a
  header-only edit still does not invalidate the stamp; delete
  `artefacts/.shared-builds/` (or run without `--share-build`) to force a
  fresh compile. See
  [Known Issues](../known-issues.md#shared-build-reuse-sees-header-edits-only-where-the-builder-reports-them)
  for what remains untracked.

## Randomization

Two seed options are available with the `test` subcommand:

- `--rnd-new`: use a randomly generated seed instead of the root config seed. The seed is saved to `artefacts/{test_name}/test.randseed`.
- `--rnd-last`: repeat the test with the seed from the last `--rnd-new` run.

For running a test many times with different seeds, use `randtest`. See the [CLI reference](../reference/cli.md#randtest).

## Logging

`rtl_buddy` writes orchestration output to `rtl_buddy.log` in the directory where it is invoked.

Per-test simulation output goes to `artefacts/{test_name}/`:

- `test.log` — full simulation output
- `test.err` — stderr
- `test.randseed` — the seed used
- `coverage.dat` — coverage database (if coverage is enabled)
- `compile.log` — compile transcript
- `run.f` — generated, non-portable filelist; explicit source entries are pinned
  to the resolved checkout with absolute paths

For repeated runs (`randtest`), each iteration writes into a numbered subdirectory — `artefacts/{test_name}/run-0001/`, `run-0002/`, etc. — while compile outputs remain at the top of `artefacts/{test_name}/`.

The symlinks `test.log`, `test.err`, and `test.randseed` at the suite root always point to the most recent run.

For machine-readable logs (JSON Lines), use `--machine`. See [For Agents](../agents.md).

## Path and working directory

`test` and `randtest` anchor outputs on the directory containing `tests.yaml`. You can run them from anywhere — invoke `rb test -c path/to/tests.yaml` and the artifact tree, `rtl_buddy.log`, and builder scratch all land under `dirname(tests.yaml)`, not your shell's cwd. See [Execution Context](execution-context.md) for the full picture and the worked example for invoking from a sibling directory.

Paths in `tests.yaml` (such as `model_path`) are resolved relative to the suite file's directory, not the invocation directory.

Plusargs are passed to the simulator verbatim. If a plusarg should reference a suite-local file, resolve it explicitly in preproc using `suite_dir`. Bare output filenames can remain artifact-relative so they land under `artefacts/{test_name}/`.

## Full schema

See [YAML Formats: tests.yaml](../reference/yaml.md#testsyaml) for the complete field reference.
