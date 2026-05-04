---
description: How to collect, merge, and report coverage for Verilator and VCS builds using rtl_buddy.
---

# Coverage

`rtl_buddy` supports coverage collection, merging, and reporting for Verilator-based builds, and merged coverage report generation for VCS builds. Use this page when you need to enable coverage in a builder mode, understand which merge/report outputs exist for each simulator family, and run the right post-processing command for your backend.

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

For VCS, configure your coverage-enabled builder mode so each test run emits a coverage database at `artefacts/<test>/simv.vdb`. The exact VCS compile/run flags are project-specific, but `rtl_buddy` expects that output location when `--coverage-merge` is used.

### Tool prerequisites

- Verilator LCOV/HTML flows require the existing Verilator coverage toolchain described below.
- VCS merged report generation requires `urg` on `PATH`.

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

For Verilator, three merge modes are available, selected by a mutually exclusive flag. Only one may be used per run.

| Flag | Merge method | Outputs |
|------|-------------|---------|
| `--coverage-merge` | Raw for summary/HTML, info-process for Coverview | summary, HTML (if `--coverage-html`), Coverview (if `--coverage-coverview`) |
| `--coverage-merge-raw` | Raw Verilator merge only | summary, HTML, Coverview |
| `--coverage-merge-info-process` | info-process only | summary, Coverview — HTML not supported |

If none of these flags is given, no merging is done and coverage is reported per test.

### VCS merge support

For VCS, `rtl_buddy` currently supports only `--coverage-merge`. It discovers per-test coverage databases at `artefacts/<test>/simv.vdb`, writes `cov_dir/coverage.f`, and runs:

```bash
urg -f coverage.f -dbname merged.vdb -report urgReport -lca -format both -show tests
```

The resulting `coverage.f`, `merged.vdb`, and `urgReport/` are written under `cov_dir/`.

Supported commands:

```bash
rtl-buddy --builder-mode cov test basic --coverage-merge
rtl-buddy --builder-mode cov regression --coverage-merge
```

On `test`, `rtl_buddy` merges across all selected tests in the current suite run. If only one eligible `simv.vdb` was produced, it still writes `coverage.f`, `merged.vdb`, and `urgReport/`.

The following flags are not supported for VCS in this first patch:

- `--coverage-merge-raw`
- `--coverage-merge-info-process`
- `--coverage-html`
- `--coverage-coverview`
- `--coverage-per-test`

## Generating merged output

### Verilator LCOV HTML report

Requires `use-lcov: true` in `cfg-coverage`. Not supported with `--coverage-merge-info-process`.

```bash
rtl-buddy --builder-mode cov regression --coverage-merge --coverage-html
```

Output is written to `coverage_merge.html` in the current directory.

### Verilator Coverview zip

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

## Full flag reference

See the [CLI reference](../reference/cli.md) for the complete flag descriptions on `test` and `regression`.

## Full schema

See [YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) for `cfg-coverage` and `cfg-coverview` schema details.
