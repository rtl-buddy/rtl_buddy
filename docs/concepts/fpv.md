---
description: How to run formal property verification with rtl_buddy via the rb fpv command, fpv.yaml, and SymbiYosys.
---

# Formal Property Verification

> **Integration type:** Integrated tool. `rb fpv` is built around [SymbiYosys (`sby`)](https://symbiyosys.readthedocs.io/) today.
>
> **External binary required:** `sby` plus at least one SMT solver (e.g. `yices`, `z3`, `boolector`) — see [Installing SymbiYosys](#installing-symbiyosys).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb fpv` drives SymbiYosys through a generated `.sby` config that consumes the model's filelist plus a list of SystemVerilog property files. It produces a per-run `status` verdict, a counterexample VCD when a property is disproved, and a full `sby` log under `<dir of fpv.yaml>/artefacts/<run>/`.

The flow is intentionally compact and config-driven — per-run knobs (mode, depth, engines, properties) live in `fpv.yaml`, tool-wide defaults live in `cfg-fpv-tools` in `root_config.yaml`.

## Supported backend

Today only `sby` is wired up. The `tool:` field in `fpv.yaml` selects it; the runner raises a clear error if no matching `cfg-fpv-tools` entry exists. Adding a second backend (e.g. a commercial formal tool) parallels how [`rb cdc` is structured](https://github.com/rtl-buddy/rtl_buddy/issues/85) — implement a sibling driver under `src/rtl_buddy/tools/`, then dispatch from `FpvRunner`.

## Installing SymbiYosys

`sby` must be on `PATH`, or its absolute path must be configured via a `cfg-fpv-tools` entry in `root_config.yaml` (see [yaml.md](../reference/yaml.md#root_configyaml)). The standalone install is small and runs on top of the Yosys you already have for `rb synth` / `rb cdc`:

```bash
git clone https://github.com/YosysHQ/sby.git
cd sby
make install PREFIX=$HOME/.local      # no sudo
pip install click                     # sby's only Python dep
```

`sby` shells out to one or more solvers — install at least one:

```bash
brew install yices2                   # macOS — most common default
brew install z3                       # widely useful
brew install boolector                # bitvector-heavy proofs
brew install berkeley-abc             # for the `abc pdr` engine
```

For a turnkey alternative, the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) bundles Yosys, `sby`, and every supported solver in one tarball.

### Version expectations

`rb fpv` probes `sby --version` and logs it as `fpv.sby_version` so the version is captured in `rtl_buddy.log`. There is no hard minimum today — anything 0.40 or newer should work.

## FPV config: `fpv.yaml`

`fpv.yaml` declares one or more verification runs. Each entry references a model from `models.yaml`, a top module (defaults to the model name), and one or more property files:

```yaml
rtl-buddy-filetype: fpv_config

verifications:
  - name: "demo_fpv_fifo"
    desc: "Bounded proof of FIFO interface assertions"
    tool: "sby"
    model: "demo_fifo"
    model_path: "../../design/demo_fifo/models.yaml"
    top: "demo_fifo"
    constraints: "shared_clock_reset.sv"     # optional
    properties:
      - "demo_fifo_props.sv"
    mode: "bmc"
    depth: 32
    engines:
      - "smtbmc yices"
    reglvl: 1000
```

### Fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — only `"sby"` is supported today |
| `model` | Model name from `models.yaml` |
| `model_path` | Path to `models.yaml`, resolved relative to `fpv.yaml` |
| `top` | Top module passed to `prep -top`; defaults to `model` when omitted |
| `properties` | List of `.sv` files containing SVA properties / bound checker modules, resolved relative to `fpv.yaml`. Optional when properties are in-RTL under `` `ifdef FORMAL `` guards |
| `constraints` | Optional path to a single `.sv` file containing environment `assume property` statements (clock toggle, reset sequence, etc.) — analogous to `constraints:` in `pnr.yaml`. Read into the sby script *before* `properties:` so the assumes are in scope when the asserts elaborate. Lets multiple verifications share one clock/reset boilerplate file instead of duplicating it across every bound checker. |
| `mode` | One of `bmc` (bounded), `prove` (k-induction), `cover`, `live` |
| `depth` | Cycle depth passed to sby; defaults to 20 |
| `engines` | List of sby engine specs (e.g. `smtbmc yices`, `abc pdr`); defaults to `["smtbmc yices"]` |
| `reglvl` | Regression level for filtering (same semantics as `rb synth` / `rb cdc`) |
| `tool_overrides` | Optional per-tool overrides for `timeout` or `extra_args`, keyed by FPV tool name |
| `vacuity` | Optional bool. When true (default for `bmc` / `prove`), run a secondary sby cover-mode pass over auto-derived cover properties for every `a \|-> b` antecedent in the property set — flags vacuous proofs. Defaults to false for `cover` / `live` modes. See [Vacuity covers](#vacuity-covers). |
| `coi` | Optional bool. When true (default), run a yosys cone-of-influence pass and report the fraction of design cells reachable from at least one assertion. See [Cone-of-influence coverage](#cone-of-influence-coverage). |
| `frontend` | SystemVerilog frontend: `"verilog"` (default — fast, no plugin, immediate-assert + simple-concurrent SVA only) or `"slang"` (yosys-slang plugin — required for concurrent SVA `\|->` / `\|=>` / sequence operators and for `bind` to elaborate). `slang` requires `cfg-fpv-tools[].opts.plugin-path` in root_config.yaml. See [Choosing a frontend](#choosing-a-frontend). |
| `xfail` | Optional bool, default false. Marks the verification as *expected to fail*, **non-strict**. See [Expected failures (xfail)](#expected-failures-xfail). |
| `xfail_strict` | Optional bool, default false. Like `xfail`, but **strict** — an unexpected pass (`XPASS`) counts as a failure. See [Expected failures (xfail)](#expected-failures-xfail). |

### Expected failures (xfail)

Mark a verification that is **known not to hold** — a teaching property
that is true but not inductive under `mode: prove`, or a placeholder for
a not-yet-fixed bug you want tracked in the suite rather than deleted.
A verification is treated as expected-to-fail when **either** `xfail` or
`xfail_strict` is set; the verdict is then re-interpreted:

| Actual outcome | Reported as | Counts as |
|---|---|---|
| FAIL | `XFAIL` | **pass** — the expected failure happened, so it does not fail the run or `rb fpv-regression` |
| PASS | `XPASS` | depends on strictness (see below) |
| SKIP / NA | unchanged | unchanged |

The two markers differ **only** in how an unexpected pass is counted:

| Marker | `XPASS` counts as | Use when |
|---|---|---|
| `xfail: true` | **pass** (non-strict) | the property *may* start passing and that is fine / not worth failing on |
| `xfail_strict: true` | **fail** (strict) | a pass means the marker is stale and you want to be told — the safe choice for a regression guard |

If both are set, strict wins. Each verification picks the marker it
needs. A common pattern: `xfail_strict: true` on a teaching demo, so the
regression turns red (via `XPASS`) the moment the property starts
holding and the marker should be removed.

Like pytest `xfail` without `raises=`, this does not distinguish a
genuine property disproof from an infrastructure error that also
surfaces as a FAIL, so reserve it for properties whose failure is
understood.

The same `xfail` / `xfail_strict` markers and `XFAIL` / `XPASS`
semantics apply to every command whose results carry a PASS/FAIL/SKIP
verdict — `tests.yaml`, `synth.yaml`, `cdc.yaml`, `pnr.yaml`, and
`power.yaml` — via one shared implementation. (`cdc.yaml` in particular
is a natural fit for a design with known/intentional CDC violations.)

### Where inputs come from

The runner reads the model's filelist via `VlogFilelist` (the same helper `rb synth` and `rb cdc` use), extracts source files and `+incdir+` entries, and emits them under the sby config's `[files]` and `[script]` sections respectively. The script reads, in order: design sources → `constraints:` (if set) → `properties:`. Putting constraints before properties ensures their `assume property` statements are in scope when the assertions are elaborated. Property files can be in-RTL with `` `ifdef FORMAL `` guards or standalone bound checker modules.

Both frontends define `FORMAL` (and not `SYNTHESIS`) when reading sources, so `` `ifdef FORMAL `` guards behave the same either way: the verilog path uses `read -sv -formal` (which swaps yosys's implicit `SYNTHESIS=1` define for `FORMAL=1`), and the slang path passes `--no-synthesis-define -DFORMAL=1` to `read_slang` for the same effect — in the main proof script, the COI script, and the vacuity pass.

## Root config: `cfg-fpv-tools`

`cfg-fpv-tools` declares the FPV tools available to all suites in this project:

```yaml
cfg-fpv-tools:
  - name: "sby"
    tool: "sby"          # binary on PATH, or absolute path
    opts:
      timeout: 600       # seconds per task; optional
      extra-args: ""     # appended to sby invocation; optional
      solver-versions:   # optional pins for reproducible CI; map
        yices: "2.6.4"   # solver name -> exact version string
        z3: "4.13.0"
```

| Field | Description |
|-------|-------------|
| `name` | Referenced by `tool:` in `fpv.yaml` |
| `tool` | Binary name (PATH-resolved) or absolute path |
| `opts.timeout` | Per-task timeout in seconds, written to the sby `[options]` block |
| `opts.extra-args` | Passed through verbatim to the sby command line |
| `opts.solver-versions` | Optional map of solver name → exact version. Probed before every run; hard-fails on mismatch. Known solvers: `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, `abc` |

### Choosing a frontend

Two SystemVerilog frontends are supported under `rb fpv`:

- **`frontend: verilog`** (default) — yosys's native verilog reader. No plugin needed. Handles immediate `assert (expr);` inside `always` blocks plus simple concurrent assertions (`assert property (@clk expr);`). Rejects `|->` / `|=>`, `##N`, sequence operators, and SV `bind`.
- **`frontend: slang`** — yosys-slang plugin. Required for concurrent SVA implications (`a |-> b`, `a |=> b`), sequence operators, and SV `bind` directives to elaborate. Requires `cfg-fpv-tools[].opts.plugin-path` in `root_config.yaml`.

When in doubt, **start with `verilog`** — it's the path most rtl_buddy demos use. Move to `slang` only when you need `|->` / `|=>` for vacuity covers or a richer SVA dialect.

#### Which yosys-slang build to use

povik's [upstream `master`](https://github.com/povik/yosys-slang) does not yet lower concurrent SVA implications (`|->` / `|=>`) to `$check` cells — the in-flight implementation lives in [povik/yosys-slang#317](https://github.com/povik/yosys-slang/pull/317), still in draft as of 2026-05-26. Using upstream master with `rb fpv` `frontend: slang` on a `|->` property surfaces as:

```
error: encountered unsupported SVA feature
```

Until #317 merges upstream, build from the **[rtl-buddy/yosys-slang `rtl-buddy` branch](https://github.com/rtl-buddy/yosys-slang/tree/rtl-buddy)** — three commits ahead of povik master (the SVA-rebase work + a stale-test count fix + a `disable iff` regression fix; ctest 46/46). The rtl-buddy fork tracks [rtl-buddy/yosys-slang#1](https://github.com/rtl-buddy/yosys-slang/issues/1) as its vendoring status; this doc switches back to recommending upstream once the fork's `master` fast-forwards to a povik release that includes the SVA work.

povik upstream master is still fine for `rb synth` and `rb cdc` with `frontend: slang` — those paths don't need the SVA implication lowering. The [rtl-buddy-project-template SETUP_OSX.md](https://github.com/rtl-buddy/rtl-buddy-project-template/blob/main/tools/yosys-slang/SETUP_OSX.md) has the per-use-case build matrix.

### Solver version pinning

`sby` happily picks whatever solver binary it finds on PATH. On CI, different runners can resolve to different versions and silently change proof outcomes — a proof that passes at depth 32 on one machine can time out on another. Set `opts.solver-versions` to lock the resolution: before each run, each pinned solver is probed (`yices-smt2 --version` etc.) and the run hard-fails with a one-shot summary of every mismatch if any pin doesn't match exactly. Resolved versions are logged as `fpv.solver_pins_resolved` so the run artefacts capture exactly what was used.

## Running FPV

```bash
# All verifications in the default ./fpv.yaml
rb fpv

# A single verification from a specific config
rb fpv demo_fpv_fifo -c fpv/demo_fifo/fpv.yaml

# List verifications without executing
rb fpv -c fpv/demo_fifo/fpv.yaml --list

# Regression across multiple fpv.yaml suites, filtered by reglvl
rb fpv-regression -c fpv_regression.yaml -l 1000
```

## Results table

A summary table prints after each run:

```
                                  FPV Results Summary
┏━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ FPV Run       ┃ Result ┃ Description                     ┃ Mode ┃ Depth ┃ Engines      ┃ Runtime ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ demo_fpv_fifo │ PASS   │ property proved (bmc, depth 32) │ bmc  │ 32    │ smtbmc yices │ 0.4s    │
└───────────────┴────────┴─────────────────────────────────┴──────┴───────┴──────────────┴─────────┘
```

- **Mode / Depth / Engines** — what was actually run, surfaced for quick triage.
- **Engine Results** — per-engine verdict mix parsed from `<workdir>/logfile.txt`'s `summary: engine_<N> (...) returned <verdict>` lines. Renders compactly: `1/1 pass (smtbmc yices)` for single-engine runs, `2/2 pass` when all of multiple engines agree, `1/2 pass (smtbmc yices won)` when only some succeed. Shows `-` when sby died before producing a logfile.
- **Runtime** — wall-clock seconds for the sby invocation.
- **Description** — `property proved (...)` on pass; on fail, the path to the counterexample VCD when available.

> **Per-property granularity is not surfaced.** Sby has no structured per-assertion output today — `fpv.xml` is JUnit-style per-task only and the prose log doesn't tag individual assertions. Per-engine status is the finest grain `rb fpv` exposes; per-property aggregation would need a yosys-frontend change to dump assertion identifiers + per-COI status.

## Cone-of-influence coverage

After every primary proof, `rb fpv` runs a structural yosys pass that:

1. Reads the same design + constraints + properties sby just used.
2. Counts total cells per module.
3. Selects every `$assert` cell and walks its cone of influence backwards (`t:$assert %ci*` — yosys's transitive input-cone operator).
4. Reports the fraction of design cells reached by at least one assertion.

Logic *outside* every property's COI is provably unverified by the property set — a direct, actionable "what's still uncovered" signal that simulation coverage doesn't give you. When the COI pass produces data, the results table grows a **COI** column:

```text
FPV Run     Result   Description                ...   COI
counter_inv PASS     property proved (bmc, depth 32)        73% (38/52)
```

- Per-module rollup lives in `FpvResults.results["coi"]["per_module"]` so machine consumers can find under-verified blocks.
- The COI pass is enabled by default (`coi: true`) and adds a few seconds to the run; disable per verification with `coi: false` in `fpv.yaml`.
- The pass uses `yosys` on `PATH`. If yosys is missing or errors the pass is logged as a warning and the COI column shows `-` — the primary proof verdict is unaffected.

Artefacts:

- `coi.ys` — generated yosys script.
- `coi.log` — full yosys log (parsed for `stat` output).

## Dead-assume detection

The same yosys pass that computes COI coverage also rolls up `$assume` cells, splitting them into those whose logic intersects with the assertion COI versus those that don't. The latter are *structurally dead* — they constrain signals no assertion ever observes, usually a sign the environment spec drifted from the property set.

When the design has any `$assume` cells, the results table grows an **Assumes** column:

```text
FPV Run     Result   Description                ...   Assumes
counter_inv PASS     property proved (bmc, depth 32)        3 used, 2 dead
```

- `N used` (all assumes are inside the assertion COI) — silent, just a sanity confirmation.
- `M used, K dead` — `K` assumes are not reachable from any assertion. Either remove them or extend the assertion set to cover the signal they constrain.

The detection is structural and conservative: it does not prove an assumption is *semantically* dead, just that yosys's elaborated graph shows no path from the assume to any assertion. In particular, assumes inside dead-code regions or untouched submodules will surface here. The detection rides on the same `coi.ys`/`coi.log` artefacts.

## Vacuity covers

A property `a |-> b` is *vacuously true* whenever the antecedent `a` never holds — the assertion passes but tells us nothing about `b`. `rb fpv` auto-derives a `cover property` for each `|->` / `|=>` antecedent in your property set and runs a secondary sby cover-mode pass to check whether each one is reachable.

The vacuity pass is enabled by default for `mode: bmc` and `mode: prove` and disabled for `mode: cover` / `mode: live` (where the user is already exploring reachability). Override with `vacuity: true` / `vacuity: false` per verification in `fpv.yaml`.

When vacuity reports any unreachable antecedent, the results table grows a **Vacuity** column:

```text
FPV Run     Result   Description                ...   Vacuity
counter_inv PASS     property proved (bmc, depth 32)        1/3 vacuous
```

- `N ok` — every antecedent reached
- `M/N vacuous` — `M` antecedents never reached → those `|->` properties are vacuously true (fix your assumptions or your stimulus)
- `K unknown` — sby's cover output didn't tag this cover either way (logfile missing, sby died)

Per-antecedent detail is preserved in `FpvResults.results["vacuity"]["covers"]` for machine consumers and reported in the log:

- `cover_vacuity_<N>_<label>: cover property (<clocking> <antecedent>);` — synthesized into `vacuity_covers.sv`
- `vacuity.log` — full secondary sby pass log
- `vacuity_workdir/` — sby workdir from the cover pass

Scope today:

- Single-line antecedents only (the most common case).
- Clocking and `disable iff` clauses on the same line as the implication are preserved.
- Sequence-valued antecedents (`(req ##2 ack) |-> done`) are extracted but treated as boolean for the cover — close enough for the reachability signal.
- Multi-line antecedents land in a follow-up.

## Artefacts

Per-run outputs land under the command root — `<dir of fpv.yaml>/artefacts/<run>/` (the artefact tree is anchored on the selected `fpv.yaml`'s directory, not your shell's cwd; see [Execution Context](execution-context.md)):

| File | Contents |
|---|---|
| `fpv.log` | Full `sby` stdout/stderr |
| `fpv.f` | Generated filelist (stripped, deduplicated) |
| `fpv.sby` | Generated sby config (the file actually handed to `sby`) |
| `sby_workdir/status` | Sby's verdict file (`PASS`, `FAIL`, `UNKNOWN`, or `ERROR`) |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample VCD on failed properties |
| `sby_workdir/engine_<N>/logfile.txt` | Per-engine log |
| `vacuity_covers.sv` | Auto-generated sidecar module with one `cover property` per `\|->` antecedent (only when the vacuity pass ran) |
| `vacuity.sby` / `vacuity.log` / `vacuity_workdir/` | Secondary sby cover-mode pass for vacuity checks |
| `coi.ys` / `coi.log` | yosys script + log for the cone-of-influence pass (only when `coi: true`) |

## Pass/fail detection

A run is PASS when `sby` writes `PASS` to `sby_workdir/status` (or returns exit code 0 when the status file is missing).

A run is FAIL when sby writes `FAIL`, `UNKNOWN`, or `ERROR`, or when it exits non-zero. The failure description points at the counterexample trace inside `sby_workdir/engine_<N>/` so the user can open it in `gtkwave`, `surfer`, or via `rb wave-fpv` (below).

SKIP is returned when the run's `reglvl` is above the `-l` filter passed to `rb fpv-regression`.

## Opening counterexamples

```bash
# Open the CEX VCD for a failed verification in the configured surfer.
rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` reads the same `fpv.yaml` (`-c`/`--fpv-config`, default `fpv.yaml`) to resolve the verification name, then opens the trace at `<dir of fpv.yaml>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` (first engine in sorted order wins when more than one produced a trace). It opens the VCD in the `cfg-surfer` entry named `surfer-default` unless you pass `--surfer <name>`. Raises if the verification has not been run yet or the proof passed (no CEX was produced).

## Out of scope (today)

- **SymbiYosys-only.** Commercial backends (JasperGold, VC Formal, OneSpin) are not yet wired up — adding them parallels the pattern documented for [SpyGlass in `rb cdc`](https://github.com/rtl-buddy/rtl_buddy/issues/85).
- **Per-property granularity.** The summary table reports the overall sby verdict, not per-assertion pass/fail. Sby's own `status.json` per task is preserved under `sby_workdir/` for users who need that detail.
- **Wide SVA coverage.** Yosys's native frontend supports a limited subset of SystemVerilog Assertions. Broader SVA coverage will land alongside the [slang frontend](https://github.com/rtl-buddy/rtl_buddy/issues/88).
