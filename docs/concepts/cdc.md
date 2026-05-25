---
description: How to run CDC lint with rtl_buddy via the rb cdc command, cdc.yaml, and the standalone rtl-buddy-cdc analyzer.
---

# CDC Lint

> **Integration type:** Pluggable — curated. `rb cdc` is built around [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc) today; a SpyGlass-style commercial backend would plug in as a sibling driver (see [issue #85](https://github.com/rtl-buddy/rtl_buddy/issues/85)).
>
> **External binary required:** `rtl-buddy-cdc` — install with `uv tool install rtl-buddy-cdc` (or `pip install rtl-buddy-cdc`).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb cdc` drives the standalone `rtl-buddy-cdc lint` CLI: it generates a filelist from the model's `models.yaml`, hands the SystemVerilog sources, SDC, and optional waivers to the analyzer, and parses the JSON report into a pass/fail verdict.

The flow is intentionally compact and config-driven — per-analysis knobs (model, SDC, waivers, frontend) live in `cdc.yaml`, tool-wide defaults live in `cfg-cdc-tools` in `root_config.yaml`.

## Supported backend

Today only `rtl-buddy-cdc` is wired up. The `tool:` field in `cdc.yaml` selects it; the runner raises a clear error if no matching `cfg-cdc-tools` entry exists. Adding a commercial backend parallels how `rb fpv` is structured — implement a sibling driver under `src/rtl_buddy/tools/`, then dispatch from `CdcRunner`.

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
| `tool` | Backend tool name — must match a `cfg-cdc-tools` entry (only `rtl-buddy-cdc` today) |
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
| `opts.sync-depth` | Default synchronizer depth, forwarded via `--sync-depth` |
| `opts.extra-args` | Passed through verbatim to the analyzer command line |

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
                       CDC Results Summary
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ CDC Analysis    ┃ Result ┃ Violations ┃ Suppressed ┃ Crossings ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ demo_cdc_full   │ PASS   │ 0          │ 3          │ 142       │
└─────────────────┴────────┴────────────┴────────────┴───────────┘
```

- **Violations** — non-waived findings parsed from `summary.violations` in the JSON report.
- **Suppressed** — waiver-matched findings, parsed from `summary.suppressed`.
- **Crossings** — total clock-domain crossings detected (informational, parsed from `summary.crossings`).

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

When the [coordination hub](hub.md) is running for the current project, every successful `rb cdc` analysis publishes its violations to the hub as a `diagnostics_set` event under the source key `rb-cdc:<analysis_name>`. The rtl-buddy-view SPA's on-canvas badge layer and `rtl-buddy-nvim`'s `rtlbuddy` diagnostics namespace light up immediately — no `rb hub send` copy-paste.

The publish step is best-effort: missing hub, no live PID, connect failure, or a malformed JSON payload all silently no-op with a debug-level log line. The CDC analysis itself is never failed by a sidecar UI being unreachable.

Re-running an analysis after a fix replaces (or clears) just that source-key slot, so a project with several analyses doesn't have one fix wiping all the others.

## Out of scope (today)

- **rtl-buddy-cdc only.** Commercial CDC tools (SpyGlass CDC, JasperGold CDC, Questa CDC) are not yet wired up — adding them follows the same pattern documented for `rb fpv`'s SymbiYosys backend.
- **Reset-domain crossing (RDC).** Reset-domain analysis is a planned extension of `rtl-buddy-cdc`; once it lands there, `rb cdc` will surface its findings alongside CDC. Today RDC overlays surface in `rb hier --rdc-annotations` via a separate analyzer pass.
