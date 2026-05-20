---
description: How to run formal property verification with rtl_buddy via the rb fpv command, fpv.yaml, and SymbiYosys.
---

# Formal Property Verification

> **Integration type:** Integrated tool. `rb fpv` is built around [SymbiYosys (`sby`)](https://symbiyosys.readthedocs.io/) today.
>
> **External binary required:** `sby` plus at least one SMT solver (e.g. `yices`, `z3`, `boolector`) — see [Installing SymbiYosys](#installing-symbiyosys).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb fpv` drives SymbiYosys through a generated `.sby` config that consumes the model's filelist plus a list of SystemVerilog property files. It produces a per-run `status` verdict, a counterexample VCD when a property is disproved, and a full `sby` log under `fpv/<run>/artefacts/`.

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
| `mode` | One of `bmc` (bounded), `prove` (k-induction), `cover`, `live` |
| `depth` | Cycle depth passed to sby; defaults to 20 |
| `engines` | List of sby engine specs (e.g. `smtbmc yices`, `abc pdr`); defaults to `["smtbmc yices"]` |
| `reglvl` | Regression level for filtering (same semantics as `rb synth` / `rb cdc`) |
| `tool_overrides` | Optional per-tool overrides for `timeout` or `extra_args`, keyed by FPV tool name |

### Where inputs come from

The runner reads the model's filelist via `VlogFilelist` (the same helper `rb synth` and `rb cdc` use), extracts source files and `+incdir+` entries, and emits them under the sby config's `[files]` and `[script]` sections respectively. Property files are appended to the same `read -sv -formal` block — they can be in-RTL with `` `ifdef FORMAL `` guards or standalone bound checker modules.

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
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ FPV Run         ┃ Result ┃ Description               ┃ Mode ┃ Depth ┃ Engines      ┃ Runtime ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ demo_fpv_fifo   │ PASS   │ property proved (bmc, 32) │ bmc  │ 32    │ smtbmc yices │ 0.4s    │
└─────────────────┴────────┴───────────────────────────┴──────┴───────┴──────────────┴─────────┘
```

- **Mode / Depth / Engines** — what was actually run, surfaced for quick triage.
- **Runtime** — wall-clock seconds for the sby invocation.
- **Description** — `property proved (...)` on pass; on fail, the path to the counterexample VCD when available.

## Artefacts

Per-run outputs land under `fpv/<run>/artefacts/`:

| File | Contents |
|---|---|
| `fpv.log` | Full `sby` stdout/stderr |
| `fpv.f` | Generated filelist (stripped, deduplicated) |
| `fpv.sby` | Generated sby config (the file actually handed to `sby`) |
| `sby_workdir/status` | Sby's verdict file (`PASS`, `FAIL`, `UNKNOWN`, or `ERROR`) |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample VCD on failed properties |
| `sby_workdir/engine_<N>/logfile.txt` | Per-engine log |

## Pass/fail detection

A run is PASS when `sby` writes `PASS` to `sby_workdir/status` (or returns exit code 0 when the status file is missing).

A run is FAIL when sby writes `FAIL`, `UNKNOWN`, or `ERROR`, or when it exits non-zero. The failure description points at the counterexample trace inside `sby_workdir/engine_<N>/` so the user can open it in `gtkwave`, `surfer`, or via `rb wave-fpv` (below).

SKIP is returned when the run's `reglvl` is above the `-l` filter passed to `rb fpv-regression`.

## Opening counterexamples

```bash
# Open the CEX VCD for a failed verification in the configured surfer.
rb wave-fpv demo_fpv_counter_safety
```

`rb wave-fpv` resolves the trace at `fpv/<suite>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` (first engine wins when more than one produced a trace). The configured surfer comes from the same `cfg-surfer` entry that `rb wave` uses; override with `--surfer <name>`. Raises if the verification has not been run yet or the proof passed (no CEX was produced).

## Out of scope (today)

- **SymbiYosys-only.** Commercial backends (JasperGold, VC Formal, OneSpin) are not yet wired up — adding them parallels the pattern documented for [SpyGlass in `rb cdc`](https://github.com/rtl-buddy/rtl_buddy/issues/85).
- **Per-property granularity.** The summary table reports the overall sby verdict, not per-assertion pass/fail. Sby's own `status.json` per task is preserved under `sby_workdir/` for users who need that detail.
- **Wide SVA coverage.** Yosys's native frontend supports a limited subset of SystemVerilog Assertions. Broader SVA coverage will land alongside the [slang frontend](https://github.com/rtl-buddy/rtl_buddy/issues/88).
