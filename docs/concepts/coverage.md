---
description: How to collect, merge, and report coverage for Verilator-based builds using rtl_buddy.
---

# Coverage

`rtl_buddy` supports coverage collection, merging, and reporting for Verilator-based builds. Coverage workflows use a dedicated builder mode to compile with instrumentation, then optionally merge results across tests and export them as LCOV HTML or Coverview packages.

## Setup

### Builder mode

Coverage instrumentation requires a builder mode that adds coverage flags at compile time. Add a `cov` mode (or similar name) to your builder entry in `root_config.yaml`:

```yaml
cfg-rtl-builder:
  - name: "verilator"
    builder: "verilator"
    builder-simv: "obj_dir/simv"
    builder-opts:
      cov:
        compile-time: >-
          --binary -sv -o simv
          --coverage
        run-time: "+verilator+rand+reset+2"
```

Run with the coverage builder mode:

```bash
rtl-buddy --builder-mode cov test basic
rtl-buddy --builder-mode cov regression
```

### Coverage config in root_config.yaml

The `cfg-coverage` section tells `rtl_buddy` how to post-process coverage output for each simulator family:

```yaml
cfg-coverage:
  - name: "verilator"
    use-lcov: true
```

`use-lcov: true` enables `.info` file export and LCOV HTML generation when `--coverage-html` is used. The `name` field must match the simulator family name used in `cfg-rtl-builder`.

### Coverview config in root_config.yaml

The optional `cfg-coverview` section configures Coverview packaging:

```yaml
cfg-coverview:
  - name: "verilator"
    generate-tables: "line"
    config:
      # inline Coverview JSON configuration values
```

Fields:

- `name`: simulator family name, matching `cfg-rtl-builder`
- `generate-tables`: coverage type to use for Coverview tables (e.g. `"line"`)
- `config`: inline dict of Coverview JSON configuration values

## Coverage merge modes

Three merge modes are available, selected by a mutually exclusive flag. Only one may be used per run.

| Flag | Merge method | Outputs |
|------|-------------|---------|
| `--coverage-merge` | Raw for summary/HTML, info-process for Coverview | summary, HTML (if `--coverage-html`), Coverview (if `--coverage-coverview`) |
| `--coverage-merge-raw` | Raw Verilator merge only | summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | summary, Coverview — HTML not supported |

If none of these flags is given, no merging is done and coverage is reported per test.

### Coverage requested but not produced

Passing any coverage output flag (`--coverage-merge`, `--coverage-merge-raw`, `--coverage-merge-info-process`, `--coverage-html`, `--coverage-coverview`, `--coverage-dir-summary`, `--coverage-dir-summary-file`) to `test` or `regression` asserts that coverage data will actually be produced. If no executed (non-skipped) test produced raw coverage data — for example, the selected builder mode lacks Verilator coverage instrumentation — the command fails with a configuration error and exits 2, instead of silently succeeding with no coverage artifact. If every selected test was skipped, no error is raised.

## Generating merged output

### LCOV HTML report

Requires `use-lcov: true` in `cfg-coverage`. Not supported with `--coverage-merge-info-process`.

```bash
rtl-buddy --builder-mode cov regression --coverage-merge --coverage-html
```

Output is written to `coverage_merge.html` in the current directory.

`--coverage-html` requires `genhtml` (the `lcov` entry in `rb tool-check`). If it is missing, the command fails with the standard dependency error — e.g. "lcov not found — run `rb tool-check --explain lcov` for install instructions" — instead of an opaque traceback. See [Tool Dependency Check](tool-check.md).

### Coverview zip

```bash
rtl-buddy --builder-mode cov regression --coverage-merge --coverage-coverview
```

In regression mode, use `--coverage-per-test` to package one Coverview dataset per test instead of merging:

```bash
rtl-buddy --builder-mode cov regression --coverage-coverview --coverage-per-test
```

## Directory-level coverage summary

Add per-directory coverage breakdowns to the summary output using `--coverage-dir-summary`. Pass repo-relative directory prefixes; the flag may be repeated.

```bash
rtl-buddy --builder-mode cov regression \
  --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem
```

Or provide prefixes from a file (one per line):

```bash
rtl-buddy --builder-mode cov regression \
  --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```

## Per-cover-point results

The `functional` metric is a single ratio: how many user cover points were hit out of how many exist. Under [`--machine`](../agents.md#machine-mode), runs also report the points individually, so a consumer can tell *which* points a suite exercised — the usual reason being to grade SVA `cover property` labels against verification-plan items.

Each entry is `{name, file, line, module, hits}`, where `name` is the cover label as written in the RTL or testbench and `module` is the module it was compiled into:

```json
{
  "name": "APB_IF_WRITE",
  "file": "../../tb_top.sv",
  "line": 89,
  "module": "tb_top",
  "hits": 13
}
```

The list appears in two places: on each result row (that test's own counts) and on the run-level `payload.coverage` (summed across every test). `file` is stored as the simulator recorded it, which is often relative to the run directory rather than the repo root.

A point instantiated several times within one module collapses to a single entry whose `hits` covers every instance, so `hits > 0` means "covered". Verilator does that folding itself — it writes one record per point per module, with the instance counts already added and the differing hierarchy component replaced by a `*` — and `rtl_buddy` folds across tests on top of that, keying on `(file, line, name, module)`.

Keeping `module` in the key matters when the same cover property is compiled into more than one module, which usually means one written in an `include`d file and pulled into several blocks. Those stay separate entries. Combining them would report a single nonzero count, hiding the case where the property is exercised in one module and never in another — a real coverage hole. If you want to grade purely by label, fold the list by `name` yourself; that direction is always available, whereas a pre-combined count cannot be split back apart.

This data comes from the per-test raw databases, not from a merged artifact, so it needs no `--coverage-merge*` flag and reads the same under `--coverage-merge` and `--coverage-merge-raw`.

!!! note "Verilator only"

    Per-cover-point results are parsed out of Verilator's raw `coverage.dat`, the only place the labels survive — `verilator_coverage --write-info` folds user coverage into anonymous LCOV records and erases the names. On other simulator families (for example VCS) the field is simply absent from the payload. Absent means "not collected", not "no points covered"; do not treat a missing list as a coverage hole.

## Full flag reference

See the [CLI reference](../reference/cli.md) for the complete flag descriptions on `test` and `regression`.

## Full schema

See [YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) for `cfg-coverage` and `cfg-coverview` schema details.
