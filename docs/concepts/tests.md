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

### Default transcript parsing

When `uvm` is **not** set, `rtl_buddy` determines the result by parsing `artefacts/{test_name}/test.log` after simulation. Your testbench must print a result marker to **stdout** at the start of a line:

- `PASS <optional detail>`
- `FAIL <optional detail>`

When emitting `FAIL`, also print an `ERR:` or `FAT:` line. The default failure parser expects one:

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
- When using `FAIL`, follow it with an `ERR:` or `FAT:` line.
- If no `PASS` or `FAIL` marker is found, `rtl_buddy` records the test as `NA` with description `test result unknown`.
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

### Exit codes

`rtl_buddy` returns one of three exit codes from test commands:

| Code | Meaning |
|------|---------|
| 0 | All tests passed |
| 1 | One or more tests failed |
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

The build directory is keyed on a hash of the compile inputs — builder
executable, compile-time options, plusdefines, compile environment, and the
resolved filelist — and lives at `artefacts/.shared-builds/obj_dir_<hash>/`.
The first test with a given key compiles; subsequent tests find a valid
`simv` and skip verilation entirely. Runtime-only inputs (plusargs, seeds,
`timeout`) never affect the key, so tests that differ only in those always
share. Tests with different `pd` plusdefines hash to different keys and
compile separately.

After a successful compile, a `rb-compile-stamp.json` recording the exact
compile inputs (including each source file's size and modification time) is
written next to the `simv`. Reuse only happens when the stamp matches, so
editing any file listed in the filelist triggers a rebuild in place.

Caveats:

- Verilator builders only. Other builders log a warning and compile per
  test as before.
- Changes inside `+incdir+` include directories are not tracked by the
  stamp; delete `artefacts/.shared-builds/` (or run without
  `--share-build`) to force a fresh compile after header-only edits.
- Toolchain upgrades are likewise invisible to the stamp. See
  [Known Issues](../known-issues.md#shared-build-reuse-does-not-see-header-edits-or-toolchain-upgrades)
  for the full list of untracked inputs.

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
- `run.f` — generated filelist

For repeated runs (`randtest`), each iteration writes into a numbered subdirectory — `artefacts/{test_name}/run-0001/`, `run-0002/`, etc. — while compile outputs remain at the top of `artefacts/{test_name}/`.

The symlinks `test.log`, `test.err`, and `test.randseed` at the suite root always point to the most recent run.

For machine-readable logs (JSON Lines), use `--machine`. See [For Agents](../agents.md).

## Path and working directory

`test` and `randtest` anchor outputs on the directory containing `tests.yaml`. You can run them from anywhere — invoke `rb test -c path/to/tests.yaml` and the artifact tree, `rtl_buddy.log`, and builder scratch all land under `dirname(tests.yaml)`, not your shell's cwd. See [Execution Context](execution-context.md) for the full picture and the worked example for invoking from a sibling directory.

Paths in `tests.yaml` (such as `model_path`) are resolved relative to the suite file's directory, not the invocation directory.

Plusargs are passed to the simulator verbatim. If a plusarg should reference a suite-local file, resolve it explicitly in preproc using `suite_dir`. Bare output filenames can remain artifact-relative so they land under `artefacts/{test_name}/`.

## Full schema

See [YAML Formats: tests.yaml](../reference/yaml.md#testsyaml) for the complete field reference.
