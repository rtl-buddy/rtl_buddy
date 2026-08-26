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
| `sweep` | Sweep expansion script (see [Plugins](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/plugins/)) |
| `preproc` | Pre-processing script (see [Plugins](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/plugins/)) |
| `assertions` | Boolean: compile in SVA (`--assert`) and report firings (see [Assertion-Based Verification](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/abv-simulation/)) |

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

Use `--reg-level` and `--start-level` on the `regression` subcommand to select a level range. See [Regressions](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/regressions/).

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
