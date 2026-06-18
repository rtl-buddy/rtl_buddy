---
description: How to run CDC lint with rtl_buddy via the rb cdc command, cdc.yaml, and the standalone rtl-buddy-cdc analyzer.
---

# CDC Lint

> **Integration type:** Pluggable — curated. `rb cdc` is built around [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc), with Vivado's `report_cdc` available as a second-opinion backend; a SpyGlass-style commercial backend would plug in as a sibling driver (see [issue #85](https://github.com/rtl-buddy/rtl_buddy/issues/85)).
>
> **External binary required:** `rtl-buddy-cdc` — install with `uv tool install rtl-buddy-cdc` (or `pip install rtl-buddy-cdc`). The optional `vivado` backend needs a Vivado install (see [FPGA Implementation](fpga.md#installing-vivado)).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb cdc` drives the standalone `rtl-buddy-cdc lint` CLI: it generates a filelist from the model's `models.yaml`, hands the SystemVerilog sources, SDC, and optional waivers to the analyzer, and parses the JSON report into a pass/fail verdict.

The flow is intentionally compact and config-driven — per-analysis knobs (model, SDC, waivers, frontend) live in `cdc.yaml`, tool-wide defaults live in `cfg-cdc-tools` in `root_config.yaml`.

## Supported backends

Two backends are registered today; the `tool:` field in `cdc.yaml` selects one, and the runner errors cleanly on an unknown name or a missing `cfg-cdc-tools` entry:

- **`rtl-buddy-cdc`** — the primary analyzer, documented through the rest of this page.
- **`vivado`** — AMD/Xilinx Vivado's `report_cdc`, a [second-opinion backend](#vivado-backend-second-opinion-not-authority).

Adding another backend (SpyGlass, Questa CDC, ...) is a wrapper class under `src/rtl_buddy/tools/` plus a one-line entry in `CdcRunner`'s backend registry.

## Vivado backend (second opinion, not authority)

With `tool: "vivado"` an analysis elaborates the model with `synth_design` (no place/route) and runs `report_cdc -details` — useful as an independent cross-check on `rtl-buddy-cdc`, or where a Vivado license is already part of the flow. Two framing rules:

- **rtl_buddy surfaces Vivado's findings; it does not adopt Vivado's ruleset as canonical.** Each finding keeps Vivado's rule id (`CDC-1`, `CDC-3`, ...), severity (`Critical`/`Warning`/`Info`), and description verbatim, tagged with `backend: "vivado"` in the machine payload. They are vendor opinions, not rtl_buddy taxonomy.
- **Severity mapping to the pass/fail surface:** `Critical` and `Warning` findings count as violations (non-zero count = FAIL); `Info` findings (e.g. `CDC-3`, a properly `ASYNC_REG`-synchronized crossing) are informational and ride along in the findings list only.

Configuration: the `cfg-cdc-tools` entry carries the device part used for elaboration (any part your Vivado install can elaborate works — it only anchors the primitive library):

```yaml
cfg-cdc-tools:
  - name: "vivado"
    tool: "vivado"                  # binary on PATH, or absolute path
    opts:
      part: "xc7a35ticsg324-1L"    # required for the vivado backend
```

```yaml
analyses:
  - name: "demo_cdc_vivado"
    desc: "Second opinion via Vivado report_cdc"
    tool: "vivado"
    model: "demo_top"
    model_path: "../../design/demo_top/models.yaml"
    constraints: "demo_top.sdc"
    tool_overrides:
      vivado:
        part: "xczu7ev-ffvc1156-2-e"   # optional per-analysis override
```

Notes:

- The SDC is read with `read_xdc` (`create_clock` and friends are valid XDC); clocks must be defined for Vivado to see domains.
- `waivers:` is not supported by this backend — a configured waiver file logs a warning and is ignored. Filter on the verbatim findings downstream instead.
- If `vivado` is not on `PATH` the analysis is reported SKIP (the backend is optional); a missing `opts.part` is a config error (exit 2).
- Artefacts land in `<suite>/artefacts/<analysis>/`: `cdc.f` (filelist), `cdc.tcl` (rendered batch script), `vivado.log`, and `cdc.rpt` (the raw `report_cdc -details` output).

## Generating CDC timing exceptions (`--emit-constraints`)

Portable CDC IP carries its synchronizers in RTL (`(* ASYNC_REG = "TRUE" *)`) but should **not** hand-author per-instance timing exceptions. `rb cdc --emit-constraints` derives them from the analysis instead: rtl-buddy-cdc already knows the verified crossing set and the reset-synchronizer set, so the exact exceptions can be generated rather than written by hand.

```bash
# Vivado XDC for the whole design (clock defs + groups + per-crossing exceptions)
rb cdc <analysis> -c cdc.yaml --emit-constraints --format xdc -o cdc_exceptions.xdc

# Scoped, IP-relative constraints (SCOPED_TO_REF) — for a reusable IP block
rb cdc <analysis> -c cdc.yaml --emit-constraints --format sdc --scoped -o ip.sdc
```

What it emits, per verified-safe crossing:

- **`set_max_delay -datapath_only`** bounded to the destination clock period — preferred over a bare `set_false_path` because it still bounds transit and skew.
- **`set_bus_skew`** additionally for multi-bit buses (width > 1, e.g. a gray-coded counter or a handshake payload), so a false-path on a bus cannot hide bit-to-bit incoherency.
- **`set_false_path`** to each reset synchronizer's first stage (the async-assert / sync-deassert path is exempt from the data-path check).

Plus, at the top level (omitted with `--scoped`, where clock framing belongs to the parent): `create_clock` echoed from the analysis SDC and `set_clock_groups -asynchronous` for the async domain relationships.

Notes:

- `--format` selects the dialect: **`sdc`** (ASIC / open flow) or **`xdc`** (Vivado, the default). The CDC-relevant subset is identical; only the header differs.
- Cell selectors are rooted instance paths (`[get_cells u_sync/*]`, the whole source domain as `[get_cells -hierarchical *]`) that resolve for a flat top-level read and, unchanged, when the `--scoped` file is applied `SCOPED_TO_REF`. `--scoped` only drops the top-level clock framing; it does not change cell addressing.
- Requires the open `rtl-buddy-cdc` engine (the vendor backend exposes no structured crossing map); naming a `tool: "vivado"` analysis is a config error.
- Generation is only correct if the analysis classifies the IP's crossings as **safe** — configure the synchronizer recognition (module names + `sync-depth`) first. Machine mode (`--machine`) returns a manifest of every emitted exception and its rationale, plus the recognition verdict.
- **Requires a recent rtl-buddy-cdc** — the structured maps come from its `--emit-domain-map` / `--emit-reset-domain-map` (newer than plain lint). An analyzer that lacks them produces no map: the command then reports `SKIP` (when the analysis otherwise passed) rather than constraints. If you expected output and got a SKIP, upgrade rtl-buddy-cdc (`uv tool upgrade rtl-buddy-cdc`).
- The generated constraints round-trip through the `--check-xdc` audit (below) for completeness / zero-over-waive verification.

## Auditing an XDC (`--check-xdc`)

`rb cdc --check-xdc <file>` audits a Vivado XDC's **CDC-relevant** exceptions against rtl-buddy-cdc's independently-derived crossing set. It is *not* a general XDC validator — pin/IO/placement/electrical correctness stays Vivado's job. Scope is strictly `create_clock` / `create_generated_clock`, `set_clock_groups -asynchronous`, `set_false_path`, `set_max_delay -datapath_only`, `set_bus_skew`; everything else (`set_property`, placement, pblocks) is ignored.

```bash
rb cdc <analysis> -c cdc.yaml --check-xdc constraints/top.xdc
rb --machine cdc <analysis> -c cdc.yaml --check-xdc top.xdc   # JSON findings
```

The audit is a diff between the XDC's exceptions and the open engine's truth, triangulated against the optional Vivado `report_cdc` backend (the open engine is the authority; the XDC is verified *against* it, not trusted):

| Finding | Severity | Meaning |
|---|---|---|
| `unconstrained_crossing` | **blocker** | a verified crossing has no covering XDC exception — the tool will time a metastable-by-design path (false confidence or a timing failure). |
| `over_waive` | **blocker** | the XDC `set_false_path` / `set_clock_groups -asynchronous` a path rtl-buddy-cdc reports as **not** safely synchronized — the constraint **masks a real metastability bug**. (A `set_max_delay` still *times* the path, so it is not an over-waive.) |
| `missing_bus_skew` | warning | a multi-bit crossing waived with a bare false-path / clock-group and no `set_bus_skew` — bit-to-bit skew incoherency is unbounded. |
| `clock_graph` | warning / info | XDC `create_clock` set disagrees with the RTL clocking (extra clock, missing clock, or period mismatch) — can silently change the derived domain set. |

A blocker finding exits non-zero. Pair it with `--emit-constraints`: a generated XDC, fed back through `--check-xdc`, audits clean (full coverage, zero over-waive).

### Recognized synchronizers (`recognized-syncs` / `--recognize-sync`)

The audit treats anything the analyzer flags as **not** safely synchronized as eligible for an over-waive. When a crossing actually goes through a synchronizer the analyzer can't see structurally — most often a **vendor macro** like `xpm_cdc_single` whose internals you blackboxed (or that elaborated as a 1-flop stub) — declare it so the audit doesn't raise a false over-waive:

```yaml
# cdc.yaml
analyses:
  - name: top
    # ...
    recognized-syncs: ["u_xpm_.*", "xpm_cdc_single"]   # instance-path regexes
```

…or per-invocation: `rb cdc top -c cdc.yaml --check-xdc top.xdc --recognize-sync 'u_xpm_.*'` (repeatable; adds to the config list).

A crossing whose instance matches is treated as a real synchronizer: a correct XDC waiver of it is **not** reported as a dangerous over-waive, but it is **still required to be constrained** (a missing exception is still an `unconstrained_crossing`). This is the portable way to handle `xpm_cdc_*`-based designs together with blackboxing the macros; true structural recognition of XPM in the analyzer is tracked upstream ([issue #315](https://github.com/rtl-buddy/rtl_buddy/issues/315)). Otherwise the audit targets the portable / `ASYNC_REG` synchronizer pattern.

## Installing rtl-buddy-cdc

```bash
uv tool install rtl-buddy-cdc    # recommended — isolated tool env
# or
uv pip install rtl-buddy-cdc     # into the project venv
```

Once installed, the binary lands on `PATH` as `rtl-buddy-cdc`. The default `cfg-cdc-tools` entry in `root_config.yaml` resolves it from `PATH`; override with an absolute path if you need a specific version.

## CDC config: `cdc.yaml`

`cdc.yaml` declares one or more CDC analyses. Each entry references a model from `models.yaml`, an SDC describing the clocks, and an optional waivers file:

```yaml
rtl-buddy-filetype: cdc_config

analyses:
  - name: "demo_cdc_full"
    desc: "Full-design CDC lint, no waivers"
    tool: "rtl-buddy-cdc"
    model: "demo_top"
    model_path: "../../design/demo_top/models.yaml"
    constraints: "demo_top.sdc"
    waivers: "demo_top_waivers.yaml"     # optional
    frontend: "slang"                    # optional — overrides default
    reglvl: 1000
```

### Fields

| Field | Description |
|-------|-------------|
| `name` | Analysis identifier used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — must match a `cfg-cdc-tools` entry (`rtl-buddy-cdc` or `vivado`) |
| `model` | Model name from `models.yaml` |
| `model_path` | Path to `models.yaml`, resolved relative to `cdc.yaml` |
| `constraints` | SDC path (required), resolved relative to `cdc.yaml` |
| `waivers` | Optional waivers YAML, resolved relative to `cdc.yaml` |
| `frontend` | Optional parser frontend — forwarded as-is to `rtl-buddy-cdc --frontend` (rtl_buddy does not validate the set so the analyzer can add frontends without an rtl_buddy release) |
| `reglvl` | Regression level for filtering, or a dict `{tool_name: level, default: level}` |
| `tool_overrides` | Optional per-tool overrides (e.g. `sync_depth`, `extra_args`), keyed by tool name |

### Where inputs come from

The runner reads the model's filelist via `VlogFilelist` (the same helper `rb synth` and `rb fpv` use), strips down to plain source paths (no `+incdir+`, no `-y`, no `-f`), and passes them as positional arguments to `rtl-buddy-cdc lint` alongside `--top`, `--sdc`, and optionally `--waivers`. The top module is taken from the model's `name:` field.

## Root config: `cfg-cdc-tools`

`cfg-cdc-tools` declares the CDC tools available to all suites in this project:

```yaml
cfg-cdc-tools:
  - name: "rtl-buddy-cdc"
    tool: "rtl-buddy-cdc"           # binary on PATH, or absolute path
    opts:
      sync-depth: 2                 # optional — passed via --sync-depth
      extra-args: ""                # optional — appended verbatim
```

| Field | Description |
|-------|-------------|
| `name` | Referenced by `tool:` in `cdc.yaml` |
| `tool` | Binary name (PATH-resolved) or absolute path |
| `opts.sync-depth` | Default synchronizer depth, forwarded via `--sync-depth` (rtl-buddy-cdc only) |
| `opts.extra-args` | Passed through verbatim to the analyzer command line (rtl-buddy-cdc only) |
| `opts.part` | Device part for `synth_design` elaboration (vivado backend only) |

**Relative paths in `extra-args`.** A relative path inside `extra-args` (e.g. `--yosys-plugin ../slang.so`, `--emit-domain-map overlays/map.json`) is resolved by the analyzer relative to the **`cdc.yaml` directory**, not the process cwd — the same anchor `constraints` and `waivers` already use. The runner forwards the config's directory as `rtl-buddy-cdc --project-root <dir>` when the installed analyzer supports it (requires the rtl-buddy-cdc release carrying [rtl-buddy-cdc#245](https://github.com/rtl-buddy/rtl-buddy-cdc/issues/245); older analyzers run without it and fall back to cwd-relative resolution). Absolute paths are always used verbatim. Author `extra-args` paths relative to `cdc.yaml`, like the other path fields.

## Running CDC

```bash
# All analyses in the default ./cdc.yaml
rb cdc

# A single analysis from a specific config
rb cdc demo_cdc_full -c cdc/demo_top/cdc.yaml

# List analyses without executing
rb cdc -c cdc/demo_top/cdc.yaml --list

# Regression across multiple cdc.yaml suites, filtered by reglvl
rb cdc-regression -c cdc_regression.yaml -l 1000
```

## Results table

A summary table prints after each run, with one row per analysis:

```
                                      CDC Lint Results Summary
┏━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ CDC Analysis  ┃ Result ┃ Description                       ┃ Violations ┃ Suppressed ┃ Crossings ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ demo_cdc_full │ PASS   │ no rule violations (3 suppressed) │ 0          │ 3          │ 142       │
└───────────────┴────────┴───────────────────────────────────┴────────────┴────────────┴───────────┘
```

- **Description** — short human-readable status (`no rule violations`, `no rule violations (N suppressed)`, or the failure reason for FAIL/SKIP rows).
- **Violations** — non-waived findings parsed from `summary.violations` in the JSON report.
- **Suppressed** — waiver-matched findings, parsed from `summary.suppressed`.
- **Crossings** — total clock-domain crossings detected (informational, parsed from `summary.crossings`).

`rb cdc-regression` prints the same columns under the title **CDC Regression Summary** with a `Reg Level: N` metadata line.

Pass `--machine` (a global flag, before the subcommand: `rb --machine cdc`) to get a JSON envelope on stdout instead of the table. Each result row carries `name`, `result`, `desc`, and — when available — `violations`, `suppressed`, and `crossings`; vivado-backend rows additionally carry `backend: "vivado"` and a `findings` list with the verbatim `{id, severity, description, depth, exception, source, destination, source_clock, destination_clock}` entries. `cdc-regression` rows additionally carry `suite`. `rb --machine cdc --list` emits a `names` array.

## Artefacts

Per-analysis outputs land under `<suite>/artefacts/<analysis>/`:

| File | Contents |
|---|---|
| `cdc.f` | Generated filelist (unrolled, deduplicated) |
| `cdc.log` | Combined stdout/stderr from both analyzer invocations |
| `cdc.txt` | Human-readable findings report |
| `cdc.json` | Machine-readable JSON report (parsed for the results table) |

`rb cdc` runs the analyzer twice per analysis — once with `--format text` for human consumption, once with `--format json` for the parsed verdict. If this becomes a hotspot, both views could be rendered from a single JSON probe; today the duplicate elaborate keeps the output decoupled.

## Pass/fail detection

A run is PASS when:
1. `rtl-buddy-cdc` exits with code 0 or 1 (1 = rule violations found, the analyzer's "ran cleanly" signal), AND
2. The JSON report parses successfully, AND
3. `summary.violations` is `0`.

A run is FAIL when violations are present, when the JSON report is missing or malformed, or when the analyzer exits with any other code (typically 2 = elaboration failure). The failure description includes the violation count and points at `cdc.log` for diagnosis.

SKIP is returned when the analysis's `reglvl` is above the `-l` filter passed to `rb cdc-regression`.

## Hub integration

When the [coordination hub](hub.md) is running for the current project, every successful `rb cdc` analysis on the `rtl-buddy-cdc` backend publishes its violations to the hub as a `diagnostics_set` event under the source key `rb-cdc:<analysis_name>`. The rtl-buddy-view SPA's on-canvas badge layer and `rtl-buddy-nvim`'s `rtlbuddy` diagnostics namespace light up immediately — no `rb hub send` copy-paste.

The publish step is best-effort: missing hub, no live PID, connect failure, or a malformed JSON payload all silently no-op with a debug-level log line. The CDC analysis itself is never failed by a sidecar UI being unreachable.

Re-running an analysis after a fix replaces (or clears) just that source-key slot, so a project with several analyses doesn't have one fix wiping all the others.

## Out of scope (today)

- **Commercial CDC signoff tools.** SpyGlass CDC, JasperGold CDC, and Questa CDC are not yet wired up — adding one is a wrapper class plus a registry entry, the same shape the vivado backend used.
- **Reset-domain crossing (RDC).** Reset-domain analysis is a planned extension of `rtl-buddy-cdc`; once it lands there, `rb cdc` will surface its findings alongside CDC. Today RDC overlays surface in `rb hier --rdc-annotations` via a separate analyzer pass.
