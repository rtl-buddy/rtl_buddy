---
description: How to run OpenROAD place-and-route with rtl_buddy via the rb pnr command, pnr.yaml, and cfg-pnr-platforms.
---

# Place-and-Route

> **Integration type:** Integrated tool. `rb pnr` is built around OpenROAD today.
>
> **External binary required:** `openroad` ≥ `25Q1` — see [Installing OpenROAD](#installing-openroad).
>
> **Optional:** `klayout` for `--gds` / `--png` streamout and rendering — see [Installing KLayout](#installing-klayout-optional).
>
> See also: [Installation — External tools by feature](../install.md#external-tools-by-feature).

`rb pnr` drives OpenROAD through a templated Tcl flow that consumes the tech-mapped netlist from an upstream `rb synth` run. It produces routed DEF, a post-route netlist + SDC, a timing report, and a DRC report under `pnr/<run>/artefacts/`.

The flow is intentionally compact and config-driven — block-level knobs live in `pnr.yaml`, technology-level knobs live in `cfg-pdks` + `cfg-pnr-platforms` in `root_config.yaml`.

## Supported backend

Today only `openroad` is wired up. The `tool:` field in `pnr.yaml` selects it; the runner skips any other value with a clear message.

## Installing OpenROAD

`openroad` must be on `PATH`, or its absolute path must be configured
via a `cfg-pnr-tools` entry in `root_config.yaml` (see
[yaml.md](../reference/yaml.md#root_configyaml)). Build from source (no
official macOS binaries):

```bash
ln -s /path/to/OpenROAD/build/bin/openroad /usr/local/bin/openroad
```

See `tools/openroad/BUILD_OSX.md` in the project template for the macOS recipe.

### Version expectations

`rb pnr` probes `openroad -version` and compares it against an internal
`MIN_OPENROAD_VERSION` (currently `25Q1`). Older builds may still work for
the basic flow but are unvalidated — a `pnr.openroad_version_below_min`
warning is emitted and the run continues. The version is also logged at
INFO as `pnr.openroad_version` so it shows up in `rtl_buddy.log`.

## Installing KLayout (optional)

KLayout is only required when streaming out GDS with `--gds` or rendering
PNGs with `--png`. The basic P&R flow works without it.

```bash
brew install --cask klayout            # macOS
# or download from https://klayout.de
```

`rb pnr` resolves `klayout` from `PATH`. If KLayout is not present,
`--gds`/`--png` logs `pnr.no_klayout` and skips streamout/render
without failing the run.

## P&R config: `pnr.yaml`

`pnr.yaml` declares one or more P&R runs. Each entry references an upstream `rb synth` entry by path + name, an SDC, and a `cfg-pnr-platforms` name from `root_config.yaml`:

```yaml
rtl-buddy-filetype: pnr_config

runs:
  - name: "demo_pnr_nangate45"
    desc: "OpenROAD P&R on Nangate45 typ corner"
    tool: "openroad"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    floorplan:
      utilization: 0.55
      aspect: 1.0
      core-margin: 2.0
    reglvl: 1000
```

### Fields

| Field | Description |
|-------|-------------|
| `name` | Run identifier used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — only `"openroad"` is supported today |
| `synth` | Name of the upstream `rb synth` entry to consume |
| `synth-path` | Path to the `synth.yaml` containing `synth`, resolved relative to `pnr.yaml` |
| `constraints` | SDC path (required), resolved relative to `pnr.yaml` |
| `platform` | `cfg-pnr-platforms` entry name |
| `floorplan.utilization` | Core utilization (0–1); 55% is a reasonable default |
| `floorplan.aspect` | Die aspect ratio; 1.0 = square |
| `floorplan.core-margin` | Margin in microns between core area and die edge |
| `reglvl` | Regression level for filtering (same semantics as `rb synth`) |

### Where inputs come from

The runner reads the upstream `synth.yaml` to find the tech-mapped netlist at `<synth_dir>/artefacts/<synth_name>/synth_netlist.v`. The top module is taken from the synth entry's `model:` field. The SDC and the PDK Liberty/LEF come from `constraints:` and the selected `cfg-pnr-platforms` entry respectively — no path duplication.

## Root config: `cfg-pdks` and `cfg-pnr-platforms`

PDK assets live in `cfg-pdks` (per-process, corners as sub-fields — see the [synthesis page](synthesis.md#pdk-and-synth-platform-configuration)). `cfg-pnr-platforms` is the P&R-side selector:

```yaml
cfg-pnr-platforms:
  - name: "nangate45_typ"
    pdk: "nangate45"
    corner: "typ"
    cts-buffer: "BUF_X4"
    routing-layers:
      signal: "metal2-metal8"
      clock:  "metal4-metal8"
```

| Field | Description |
|-------|-------------|
| `name` | Referenced by `platform:` in `pnr.yaml` |
| `pdk` | `cfg-pdks` entry name |
| `corner` | Corner from the PDK used for STA; defaults to the first declared corner |
| `cts-buffer` | Standard cell name passed to `clock_tree_synthesis -root_buf` / `-buf_list` |
| `routing-layers.signal` | Layer range for signal routing (e.g. `metal2-metal8`) |
| `routing-layers.clock` | Layer range for clock routing (typically higher metals) |

## Running P&R

```bash
# All runs in the default ./pnr.yaml
rb pnr

# A single run from a specific config
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml

# Reglvl-gated runs (1000 by default for tech-mapped flows)
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000

# List runs without executing
rb pnr -c pnr/demo/pnr.yaml --list

# Stream out a routed GDS via KLayout after the run
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds

# Render a PNG of the GDS (implies --gds)
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
```

### `--gds` and `--png`

When `--gds` is requested, `rb pnr` invokes KLayout headlessly after a
successful OpenROAD run, merging the routed DEF with the PDK's standard
cell GDS to produce `artefacts/<run>/<design>.gds`. `--png` additionally
renders a 2048×2048 PNG via the bundled `gds2png.py` helper. Both helpers
ship inside the wheel under `rtl_buddy/pnr/klayout/` and are copied into
the artefact dir at run time. KLayout failures emit `pnr.gds_failed` /
`pnr.png_failed` warnings but do not fail the P&R run — timing/DRC
metrics remain authoritative.

## Results table

A summary table prints after each run:

```
                              P&R Results Summary
┏━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┓
┃ P&R Run  ┃ Result ┃ Desc     ┃ Cells ┃ Area    ┃ WNS Setup┃ WNS Hold ┃ DRCs ┃
┡━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━┩
│ demo_…   │ PASS   │ P&R …    │ 1392  │ 3213 µm²│ +4.350 ns│ +0.080 ns│ 0    │
└──────────┴────────┴──────────┴───────┴─────────┴──────────┴──────────┴──────┘
```

- **Cells** — `Number of instances` from OpenROAD's floorplan log.
- **Area** — `Design area … um^2` from `report_design_area`.
- **WNS Setup / WNS Hold** — `report_worst_slack -max` / `-min`.
- **DRCs** — non-empty line count of `route.drc.rpt`. Zero == clean route.

## Artefacts

Per-run outputs land under `pnr/<run>/artefacts/`:

| File | Contents |
|---|---|
| `pnr.log` | Full OpenROAD log |
| `pnr.tcl` | Templated Tcl handed to OpenROAD |
| `<design>.def` | Routed DEF |
| `<design>.routed.v` | Post-route gate-level netlist |
| `<design>.routed.sdc` | Post-route SDC |
| `timing.rpt` | Worst-path timing report (full clock expanded) |
| `route.drc.rpt` | DRC violations (empty file = clean) |
| `route.maze.log` | Detail-route maze log |
| `<design>.gds` | Routed GDS — only when `--gds`/`--png` is set |
| `<design>.png` | Layout render — only when `--png` is set |
| `klayout.def2stream.log` | KLayout output for the DEF→GDS step (when used) |
| `klayout.gds2png.log` | KLayout output for the GDS→PNG render (when used) |

## Pass/fail detection

A run is PASS when:
1. `openroad` exits with code 0.
2. The log has no `[ERROR ...]` lines.

Otherwise FAIL is returned with the exit code or error count in the description. SKIP is returned when the run's `reglvl` is above the `-l` filter or when `tool:` is not `openroad`.

## Out of scope (today)

- Multi-corner signoff.
- Tape-out-grade PPA tuning. The defaults are calibrated for teaching demos and quick PPA sanity checks.
