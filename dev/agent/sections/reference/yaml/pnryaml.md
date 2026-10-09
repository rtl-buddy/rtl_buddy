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
| `pin-constraints` | Optional | Tcl file relative to `pnr.yaml`, sourced immediately before pin placement, which runs after macro placement, the PDN and `floorplan.pins`. A missing file fails the run |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `pdn-config` | Optional | Tcl file relative to `pnr.yaml` that replaces the PDK's `pdn-config` for this run. A missing file fails the run at setup |
| `pdn` | Optional | Declarative core power grid that replaces the core grid of the pdn-config in effect. See below |
| `desc` | Required | Human-readable description |
| `lef-paths` / `lib-paths` | Optional | Design-specific macro files relative to `pnr.yaml` |
| `gds-paths` | Optional | Layout of the macros `lef-paths` names, relative to `pnr.yaml`. P&R never reads it; KLayout stream-out does |
| `blocks` | Optional | Hardened blocks instanced as hard macros. Each has `name` (the module as instanced), `pnr` (a `harden: true` run), and optional `pnr-path` (its `pnr.yaml`, relative to this one; default this file). The abstract's LEF, Liberty, and GDS are appended to `lef-paths`, `lib-paths`, and `gds-paths`. Needs a single-corner platform. See [Assemble hardened blocks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#assemble-hardened-blocks) |
| `gds-mode` | Default `preview` | `strict` fails the run when a requested export is not delivered complete. `preview` keeps an incomplete layout and reports it. `--gds-mode` overrides |
| `gds-allow-empty` | Optional | Cell names or `fnmatch` globs, matched case-sensitively, that are empty on purpose. Such a cell is not missing in either mode |
| `threads` | Default unset (1) | OpenROAD worker threads: a positive integer, or `auto` for the CPUs of the current allocation (1 outside one). Clamped with a warning to a detected Slurm or affinity allocation. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads) |
| `detailed-route-verbose` | Default 1 | `detailed_route -verbose` level, a non-negative integer. 1 logs each iteration's progress and violation count to `pnr.log`; 0 silences them; higher levels add more router detail. Not part of a hardened block's config digest. See [Follow routing progress](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#follow-routing-progress) |
| `checkpoints` | Default `false` | `true`, a stage name, or a list of `floorplan`, `place`, `cts`, `global_route`. Writes a stage-named ODB, DEF, and SDC (plus route guides and segments after `global_route`) under `artefacts/<run>/checkpoints/<run-id>/`, with a manifest and a `progress.jsonl` of step events. `[]` keeps progress only. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) |
| `harden` | Default `false` | Publishes the routed result as a hard-macro abstract under `artefacts/<run>/abstract/`: `<top>.lef`, `<top>.lib` (OpenSTA timing model), `<top>.gds`, and `abstract.manifest.json`. Implies `--gds` with `gds-mode: strict`; needs a single-corner platform. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) |
| `buffer-ports` | Default: the value of `harden` | `true` runs `buffer_ports -inputs -outputs` after IO pin placement and before global placement, so each port drives or is driven by one buffer. Part of a hardened block's config digest when `true`. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) |
| `fail-on-electrical` | Default `false` | Fails a routed run with any max-slew, max-capacitance or max-fanout violator. Without it such a run passes, qualified with the counts. Not part of a hardened block's config digest. See [Interpret results](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#interpret-results) |
| `floorplan.utilization` | Default 0.55 | Core utilization from 0 to 1 |
| `floorplan.aspect` | Default 1.0 | Die aspect ratio |
| `floorplan.core-margin` | Default 2.0 | Core-to-die margin in microns |
| `floorplan.die-area` / `floorplan.core-area` | Optional | `[x0, y0, x1, y1]` in microns, set together, core inside die. They size the floorplan exactly and exclude `utilization`, `aspect` and `core-margin`. See [Size the die and core](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#size-the-die-and-core) |
| `floorplan.core-cutouts` | Optional | `[x0, y0, x1, y1]` rectangles carved out of the core for an L, T or other rectilinear core: hard blockages with their rows cut. Inside `core-area` when it is set |
| `floorplan.macro-anchor` | Default `lower-left` | Core corner the macro packer starts from: `lower-left`, `lower-right`, `upper-left`, or `upper-right`. Cannot be set with `macro-placement: rtl-mp`. See [Floorplan controls](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls) |
| `floorplan.macro-placement` | Default `pack` | Who places hard macros: `pack` (rtl_buddy's size-aware packer) or `rtl-mp` (OpenROAD's `rtl_macro_placer`, which keeps macros out of every blockage type). See [RTL-MP macro placement](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#rtl-mp-macro-placement) |
| `floorplan.blockages` | Optional | List of standard-cell placement blockages. See below |
| `floorplan.macros` | Optional | Per-macro directives: a fixed `location`, an `orientation`, or a standard-cell `halo`. See below |
| `floorplan.pins` | Optional | List of IO pin constraints: ports by name or glob on a side, within a range, as a group, or one pin at an exact location. See below |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Each `floorplan.blockages` entry has:

- `rect: [x0, y0, x1, y1]` in microns, in die coordinates, non-negative, with `x0 < x1` and `y0 < y1`.
- `type: hard` (default), `soft`, or `partial`.
- For `partial` only, `max-density` strictly between 0 and 1. Only global placement honors it; legalization clears a partial blockage like a hard one.

Blockages need OpenROAD 26Q1 or later. Macros are kept out of `hard` blockages, and the rows under them are cut before tap insertion. See [Placement blockages](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls).

`pdn` has:

- `ring` (optional): `layers: [horizontal, vertical]`, `width`, `spacing`, `offset` in microns. `offset + 2 x width + spacing` must fit between core and die.
- `stripes` (required): each `layer` and `width`, plus either `followpins: true` or a `pitch` with optional `offset` and `spacing`.
- `connect` (optional): `[lower, upper]` layer pairs; default each stripe layer to the next.

It needs a pdn-config (the PDK's or the run's) for the global connections and voltage domain. See [Plan the power grid per run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#plan-the-power-grid-per-run).

Each `floorplan.macros` entry has an `instance` (a full instance name, or a glob over instance names) and at least one of:

- `location: [x, y]`, the lower-left corner in die microns. Snapped to the site grid, it must fit inside the core and clear of other fixed macros. The macro is fixed before macro placement and is a keep-out for the packer. The instance pattern must match exactly one macro.
- `orientation: R0`, `R180`, `MX` or `MY`. Under `macro-placement: rtl-mp` it needs a `location`.
- `halo: [x, y]` in microns, non-negative, replacing `placement.macro-cell-halo` for the matched macros.

A pattern that matches no instance, or a standard cell, fails the run. See [Place individual macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-individual-macros).

Each `floorplan.pins` entry has `names` (port names or globs, a string or a list) and one of:

- `side: left`, `right`, `top` or `bottom`, with optional `start` and `end` in die microns along that edge (`start < end`, both non-negative).
- `group: true`, optionally with `order: true`, alone or with a `side`.
- `location: [x, y]`, the pin centre in die microns, for a single port, with optional `layer` (default: the PDK pin layer of the nearest edge) and `size: [width, height]` in microns (default: the layer's minimum).

A name or glob that matches no port fails the run. A `location` pin is fixed and placed first, before the side and group constraints and the `pin-constraints` file. See [Constrain boundary pins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#constrain-boundary-pins).

The run consumes `<synth dir>/artefacts/<synth>/synth_netlist.v`. The selected PDK and platform provide Liberty, LEF, site, tie and fill cells, CTS buffer, and routing layers.

With `--gds`, KLayout stream-out reads the PDK's `cell-gds` plus the run's `gds-paths`, and is given the technology LEF, the PDK macro LEF, and the run's `lef-paths`. A configured input that is missing stops the export. `gds-mode` decides whether a cell with no layout fails the run or is reported as an incomplete preview. `rb pnr-export` reads the same keys over an already routed result. See [Place and Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-inputs), [Stream-out completeness](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-completeness), and [Export a saved result](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result).
