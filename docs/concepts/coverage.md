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

#### Status of Coverview packaging

`--coverage-coverview` is an **optional export**, not the way to look at coverage. It is kept, not deprecated, and not removed — but it is no longer on the path anything else depends on.

Why it stays:

- It is the only route to a **self-contained archive** you can hand to someone with no rtl_buddy checkout — a sign-off attachment, a CI artefact, a mail to a customer. Nothing first-party replaces that.
- Deleting it would break projects that already publish those zips from CI, for no gain: the packaging code is small and its inputs (the merged `.info`, the `.desc` files) are produced anyway.

Why it is no longer the answer to "let me look at coverage":

- Rendering the per-type attribution in it needs a **forked** Coverview checkout — our `covrby_*.desc` files render nowhere else.
- The packaging path shells out to an **unpinned external `info-process` binary** that silently no-ops when absent, and that path has no CI coverage.
- The [`/cov` pane](#looking-at-coverage-the-cov-pane) now renders the same per-type, per-test attribution first-party, offline, from the [coverage model](#the-coverage-model), and [`rb cov`](#reading-coverage-back-rb-cov) answers the same questions in a terminal or from an agent with nothing installed at all.

If you are reaching for `--coverage-coverview` to *read* coverage, use `rb cov summary` / the `/cov` pane instead. Reach for it when you need a file to send someone.

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

## Coverage artefacts: `cov_dir/manifest.json`

Every run that produces coverage writes one **manifest** beside its artefacts, at `<command root>/cov_dir/manifest.json`. It is the discovery contract: what this run produced, and where. Without it, finding last night's coverage meant knowing the suite basename, the merge mode and the directory the command happened to run from.

The manifest is written whether or not you passed a merge flag, and whether or not Coverview packaging ran — the raw databases exist either way, and so does the question "what did this cover".

```json
{
  "schema_version": 1,
  "generator": "rtl-buddy 6.24.0",
  "generated_at": "2026-08-06T11:04:12+08:00",
  "command": "regression",
  "suite": "verif/blk/regression.yaml",
  "builder": "verilator",
  "simulator_family": "verilator",
  "merge_mode": "raw",
  "cov_dir": "verif/blk/cov_dir",
  "model": "verif/blk/cov_dir/coverage-model.json",
  "totals": {"line": {"found": 412, "hit": 388, "ratio": 0.941}, "...": {}},
  "merged":       {"info": "...", "raw": "...", "desc": "...", "html_dir": "..."},
  "datasets":     {"line": "...", "branch": "...", "toggle": "...", "expression": "..."},
  "descriptions": {"line": "...", "branch": "...", "toggle": "...", "expression": "..."},
  "coverview":    {"zip": "...", "per_test_zip": "..."},
  "tests": [
    {"name": "basic", "suite": "verif/blk/tests.yaml",
     "raw": "verif/blk/artefacts/basic/coverage.dat",
     "info": "verif/blk/cov_dir/basic.coverage.info",
     "html_dir": null, "coverview_zip": null}
  ]
}
```

Two rules the file keeps:

- **Stable keys.** The `merged`, `datasets`, `descriptions`, `coverview` and `tests` blocks are always present. A value is `null` when that artefact was not produced — absent never means "not produced".
- **Project-relative paths.** Every path is POSIX and relative to the project root, so a manifest survives being read from elsewhere, archived, or attached to a CI artefact. A path that genuinely lives outside the project (a scratch filesystem) is kept verbatim rather than turned into a `../..` chain nothing can join on.

`merge_mode` is `"raw"`, `"info_process"` or `null`, naming what produced `merged.info`. `descriptions` are the per-type `.desc` attribution files `info-process` writes; they are indexed whether or not a Coverview archive was packaged.

## The coverage model

`cov_dir/coverage-model.json` is the structured half: per file, per point, per test. Its shape is simulator-agnostic — a file holds points, a point has hits and an attribution — even though Verilator's raw database plus its LCOV export is the only producer today.

```json
{
  "schema_version": 1,
  "simulator": "verilator",
  "totals": {"line": {"found": 3, "hit": 3, "ratio": 1.0}, "...": {}},
  "counts": {"files": 2, "tests": 2, "modules": 2},
  "modules": {"blk": ["design/blk.sv"]},
  "tests": [{"name": "basic", "suite": "...", "raw": "...", "info": "...",
             "totals": {"...": {}}}],
  "files": [
    {
      "path": "design/blk.sv",
      "modules": ["blk"],
      "totals": {"...": {}},
      "line":       [{"line": 2, "hits": 7, "tests": {"basic": 0, "extra": 7}}],
      "branch":     [{"line": 12, "column": 5, "name": "if", "module": "blk",
                      "hits": 0, "tests": {"basic": 0}}],
      "toggle":     [{"line": 2, "column": 9, "name": "q[0]", "module": "blk",
                      "hits": 0, "tests": {"basic": 0}}],
      "expression": [{"line": 3, "column": 1, "name": "a && b", "module": "blk",
                      "hits": 4, "tests": {"basic": 4}}],
      "cover":      [{"line": 4, "column": 1, "name": "BLK_WRITE", "module": "blk",
                      "hits": 3, "tests": {"basic": 3}}]
    }
  ]
}
```

Three properties are the point of it:

- **Per point, not per percentage.** "Which lines are cold in this block" is a read, not a re-run.
- **Attribution is unconditional.** Every point carries the per-test hit counts behind it — the same data Coverview's `.desc` files carry, except it is built whenever per-test artefacts exist rather than only when packaging an archive. That is what answers "which test covered this line", and its inverse, "what would I lose by dropping this test".
- **Paths are project-relative**, via the one source-path resolver, so a model stays meaningful when the run directory is gone.

Toggle and expression detail exists **only** in the raw database: `verilator_coverage --write-info` folds toggle, expression and user points into anonymous `DA:` records, erasing the signal names, the expression terms and the SVA labels alike. The model reads the raw `.dat` first for exactly that reason and falls back to the `.info` (line and branch only, no names) when a test's database is gone.

Points are keyed on the line alone for line coverage — a source line is hit or it is not — and on `(line, column, name, module)` for everything else, because several toggle points share a line (one per bit), several branch arms share a line, and one cover property compiled into two modules is two points, not one.

## Reading coverage back: `rb cov`

`rb cov` operates on artefacts already on disk. No simulator runs; nothing is written. With no `--cov-dir` it reads the newest `cov_dir/manifest.json` under the project root.

```bash
rb cov summary                       # run + per-test scalars, coldest files first
rb cov summary --limit 0             # every file
rb cov module blk                    # uncovered points in one module, with their tests
rb cov module blk --all              # every point, hit or not
rb cov summary --cov-dir verif/blk/cov_dir
```

`rb cov module` takes the module name as the coverage model records it (Verilator's containing module, from the record's `page` key). An unknown name exits 2 and reports near misses rather than answering about a different module. A file included into several modules reports only the points belonging to the module asked for — reporting the whole file would attribute another block's misses to this one.

### Machine artefacts

Both verbs emit the standard [envelope](../agents.md#machine-mode). Every payload carries `schema_version`, `manifest`, `generated_at`, `run_command` (the `rb` command that produced the coverage — not the verb being run, which is already the envelope's `command`), `suite`, `builder`, `simulator`, `merge_mode`, `totals`, and an `artefacts` block:

```json
"artefacts": {
  "manifest": "verif/blk/cov_dir/manifest.json",
  "cov_dir": "verif/blk/cov_dir",
  "model": "verif/blk/cov_dir/coverage-model.json",
  "merged_info": "verif/blk/cov_dir/coverage_merged.info",
  "merged_raw": "verif/blk/cov_dir/coverage_merged.dat",
  "merged_desc": "verif/blk/cov_dir/coverage_merged.desc",
  "html_dir": "verif/blk/coverage_merge.html",
  "datasets": {"line": "...", "branch": "...", "toggle": "...", "expression": "..."},
  "descriptions": {"line": "...", "branch": "...", "toggle": "...", "expression": "..."},
  "coverview_zip": null,
  "coverview_per_test_zip": null
}
```

Then:

- `cov summary` adds `counts`, `tests` (per-test `{name, suite, totals}`), `files` (coldest first — lowest line ratio, then most absolute misses, with files that have no line points at all last, since those are silent rather than cold — truncated to `--limit`), `modules`, and `covers` when the run recorded SVA cover points.
- `cov module` adds `module`, `files` (each with its per-metric point lists) and `tests` (the tests that touched any of its points).

The same `artefacts` block rides on `payload.coverage` of a `test` or `regression` run, so an orchestrator learns where the artefacts landed from the run itself, not from scraping `Merged LCOV: <path>` out of the summary lines. `payload.coverage.merged` gained an `expression` scalar alongside `line`/`branch`/`toggle`/`functional`; the `L/B/T/F` summary string is unchanged, since it is a display contract and expression detail belongs in the model where a consumer can act on it.

An unanswerable question exits 2 with `payload.error` and, for an unknown module, `payload.candidates`.

### From an agent: the MCP tools

`rb mcp` serves the same two verbs as the `cov_summary` and `cov_module` tools, calling the same payload builders — the answer an agent gets over MCP is byte-for-byte the `payload` of `rb --machine cov summary|module`, wrapped in `{tool, ok, meta, payload}`. Both take optional `cov_dir` and `manifest` arguments, exactly as the CLI does, and an unknown module comes back as `ok: false` with `candidates` instead of an exception.

They are **stateless** tools, listed whether or not a hub is running: coverage artefacts are files, so a CI or dispatch node answers "what is cold in `blk`?" with no daemon. When a hub *is* live, one more tool appears — `cov_focus`, the MCP face of `rb hub send cov-focus` — so an agent can point the open `/cov` pane at the file, module, test or point it is talking about. See [The MCP Server](graph.md#the-mcp-server).

## Looking at coverage: the `/cov` pane

`rb hub start --serve-viewer` serves the same model as an interactive page at `GET /cov`: a dashboard of the run's scalars, a file list ranked coldest-first **for the metric you pick** (it opens on `toggle`), and **per-file source annotation** — a column per metric under a header of that file's totals, each line's points summarised in it, and the per-test attribution behind every one of them in a docked detail panel. Selecting a test turns it into a lens, so every number becomes that test's contribution.

It is a hub peer, so it drives the rest: clicking a line broadcasts `source_focused` (which the hub resolves into a selection in the schematic) and opens the line in your editor; clicking a module chip focuses that module in the graph pane. `rb hub send cov-focus <target>` drives it from the other direction, and works before the browser tab is open. See [Coverage pane](hub.md#coverage-pane) for the routes and the wire types.

## Coverage on the design graph

`rb graph results` correlates what a suite **declared** it covers (`covers:` entries in `tests.yaml`, which the graph carries as `test --covers--> covitem:<block>#<id>` edges) with what this model **observed**, and writes the verdict into the results overlay: per spec item `exercised` / `declared-only` / `observed-but-undeclared`, per module a ratio, per test the scalars above.

Nothing is re-run — the numbers are read out of `cov_dir/manifest.json` and the model it names. The `/graph` pane tints the design column with them and `rb graph explain` returns them. See [Coverage on the Graph](graph.md#coverage-on-the-graph).

## Full flag reference

See the [CLI reference](../reference/cli.md) for the complete flag descriptions on `test`, `regression` and `cov`.

## Full schema

See [YAML Formats: root_config.yaml](../reference/yaml.md#root_configyaml) for `cfg-coverage` and `cfg-coverview` schema details.
