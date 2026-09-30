## pnr.yaml

Required keys are `rtl-buddy-filetype: pnr_config` and `runs`.

```yaml
rtl-buddy-filetype: pnr_config
runs:
  - name: demo_pnr
    desc: OpenROAD place and route
    tool: openroad
    synth: demo_synth
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan: {utilization: 0.55, aspect: 1.0, core-margin: 2.0}
    lef-paths: [../../pdk/sram/sram.lef]
    gds-paths: [../../pdk/sram/sram.gds]
    gds-mode: strict
    gds-allow-empty: [fakeram45_*]
    reglvl: 1000
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `tool` | Default `openroad` | Backend |
| `synth` | Required | Upstream synthesis entry |
| `synth-path` | Required | Upstream `synth.yaml`, relative to `pnr.yaml` |
| `constraints` | Required | SDC path relative to `pnr.yaml` |
| `pin-constraints` | Optional | Tcl file relative to `pnr.yaml`, sourced immediately before pin placement, which runs after macro placement and the PDN. A missing file fails the run |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `desc` | Required | Human-readable description |
| `lef-paths` / `lib-paths` | Optional | Design-specific macro files relative to `pnr.yaml` |
| `gds-paths` | Optional | Layout of the macros `lef-paths` names, relative to `pnr.yaml`. P&R never reads it; KLayout stream-out does |
| `blocks` | Optional | Hardened blocks instanced as hard macros. Each has `name` (the module as instanced), `pnr` (a `harden: true` run), and optional `pnr-path` (its `pnr.yaml`, relative to this one; default this file). The abstract's LEF, Liberty, and GDS are appended to `lef-paths`, `lib-paths`, and `gds-paths`. Needs a single-corner platform. See [Assemble hardened blocks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#assemble-hardened-blocks) |
| `gds-mode` | Default `preview` | `strict` fails the run when a requested export is not delivered complete. `preview` keeps an incomplete layout and reports it. `--gds-mode` overrides |
| `gds-allow-empty` | Optional | Cell names or `fnmatch` globs, matched case-sensitively, that are empty on purpose. Such a cell is not missing in either mode |
| `threads` | Default unset (1) | OpenROAD worker threads: a positive integer, or `auto` for the CPUs of the current allocation (1 outside one). Clamped with a warning to a detected Slurm or affinity allocation. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads) |
| `checkpoints` | Default `false` | `true`, a stage name, or a list of `floorplan`, `place`, `cts`, `global_route`. Writes a stage-named ODB, DEF, and SDC (plus route guides and segments after `global_route`) under `artefacts/<run>/checkpoints/<run-id>/`, with a manifest and a `progress.jsonl` of step events. `[]` keeps progress only. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) |
| `harden` | Default `false` | Publishes the routed result as a hard-macro abstract under `artefacts/<run>/abstract/`: `<top>.lef`, `<top>.lib` (OpenSTA timing model), `<top>.gds`, and `abstract.manifest.json`. Implies `--gds` with `gds-mode: strict`; needs a single-corner platform. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) |
| `floorplan.utilization` | Default 0.55 | Core utilization from 0 to 1 |
| `floorplan.aspect` | Default 1.0 | Die aspect ratio |
| `floorplan.core-margin` | Default 2.0 | Core-to-die margin in microns |
| `floorplan.macro-anchor` | Default `lower-left` | Core corner the macro packer starts from: `lower-left`, `lower-right`, `upper-left`, or `upper-right`. Cannot be set with `macro-placement: rtl-mp`. See [Floorplan controls](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls) |
| `floorplan.macro-placement` | Default `pack` | Who places hard macros: `pack` (rtl_buddy's size-aware packer) or `rtl-mp` (OpenROAD's `rtl_macro_placer`, which keeps macros out of every blockage type). See [RTL-MP macro placement](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#rtl-mp-macro-placement) |
| `floorplan.blockages` | Optional | List of standard-cell placement blockages. See below |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Each `floorplan.blockages` entry has:

- `rect: [x0, y0, x1, y1]` in microns, in die coordinates, non-negative, with `x0 < x1` and `y0 < y1`.
- `type: hard` (default), `soft`, or `partial`.
- For `partial` only, `max-density` strictly between 0 and 1. Only global placement honors it; legalization clears a partial blockage like a hard one.

Blockages need OpenROAD 26Q1 or later. Macros are kept out of `hard` blockages.

The run consumes `<synth dir>/artefacts/<synth>/synth_netlist.v`. The selected PDK and platform provide Liberty, LEF, site, tie and fill cells, CTS buffer, and routing layers.

With `--gds`, KLayout stream-out reads the PDK's `cell-gds` plus the run's `gds-paths`, and is given the technology LEF, the PDK macro LEF, and the run's `lef-paths`. A configured input that is missing stops the export. `gds-mode` decides whether a cell with no layout fails the run or is reported as an incomplete preview. `rb pnr-export` reads the same keys over an already routed result. See [Place and Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-inputs), [Stream-out completeness](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-completeness), and [Export a saved result](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result).
