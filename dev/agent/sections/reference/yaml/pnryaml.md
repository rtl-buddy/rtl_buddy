## pnr.yaml

Required keys are `rtl-buddy-filetype: pnr_config` and `runs`.

An unknown key in a run or in any mapping nested in it (`floorplan`, `floorplan.blockages[]`, `floorplan.pins[]`, `floorplan.macros[]`, `pdn`, `pdn.ring`, `pdn.stripes[]`, `abstract`, `placement`, `global-route-repair`, `post-route-repair` and their `repair-design`, `resources`, `blocks[]`) is a config error. The message names the file, the run, the block's position, the key, the nearest valid key and every valid key, for example `pnr run 'top': floorplan.blockages[2]: unknown key 'lifetme', did you mean 'lifetime'?`. Every unknown key is reported at once. `hooks` rejects an unknown point, and a planner file rejects an unknown entry key.

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
| `hooks` | Optional | Tcl files sourced at fixed points of the flow: a mapping of point to one path or a list of paths, relative to `pnr.yaml`. Points: `session`, `after-floorplan`, `macro-placement`, `after-pdn`, `after-pins`, `after-global-placement`, `after-repair-design`, `after-cts`, `after-global-route`, `before-detailed-route`, `before-reports`. An unknown point or a malformed value is a config error; null or an empty list is unset. A `macro-placement` hook may not be combined with `floorplan.macro-placement` or `floorplan.macro-anchor`. A missing file fails the run at setup. Part of a hardened block's config digest only when set. See [Run Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks) |
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
| `checkpoints` | Default `false` | `true`, a stage name, or a list of `floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair` (kept only with `post-route-repair`). Writes a stage-named ODB, DEF, and SDC (plus route guides and segments after `global_route`) under `artefacts/<run>/checkpoints/<run-id>/`, with a manifest and a `progress.jsonl` of step and stage-resource events. `[]` keeps progress only. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) |
| `stages` | Default `false` | `true` runs each stage (`floorplan` to `finish`) in its own OpenROAD session from the previous stage's checkpoint, writing every checkpoint and each session's script and log; such a run can be resumed with `rb pnr --resume`. A `checkpoints` list that leaves out a stage other than `post_route_repair` is an error. See [Run stages as separate sessions](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions) |
| `resources` | Optional | The Slurm reservation of each stage job under `rb pnr --dispatch slurm`: `cpus` (positive), `mem` and quoted `time`, over `cfg-dispatch.resources` field by field. `stages:` maps a stage name (`floorplan` to `finish`, including `post_route_repair`) to its own `cpus`, `mem` and `time`, over the run's. Ignored by a run that is not dispatched; not part of a hardened block's config digest. See [Dispatch the stages to Slurm](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm) |
| `harden` | Default `false` | Publishes the routed result as a hard-macro abstract under `artefacts/<run>/abstract/`: `<top>.lef`, `<top>.lib` (OpenSTA timing model), `<top>.gds`, and `abstract.manifest.json`. Implies `--gds` with `gds-mode: strict`; needs a single-corner platform. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) |
| `abstract` | Optional, needs `harden: true` | Obstruction style of the abstract LEF. `bloat`: `occupied` (default, `write_abstract_lef -bloat_occupied_layers`) or a non-negative number (`-bloat_factor`). `feedthrough-layers`: a list of layers written with the block's real shapes as obstructions while the others keep the `bloat` style. Part of a hardened block's config digest only when not the default. See [Choose the abstract's obstruction style](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#choose-the-abstracts-obstruction-style) |
| `buffer-ports` | Default: the value of `harden` | `true` runs `buffer_ports -inputs -outputs` after IO pin placement and before global placement, so each port drives or is driven by one buffer. Part of a hardened block's config digest when `true`. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) |
| `placement` | Optional | Any `placement.*` key of `cfg-pnr-platforms`, overriding the platform's resolved value field by field for this run. The resolved values must still pass the platform's checks |
| `global-route-repair` / `post-route-repair` | Optional | The platform's block of the same name, overridden field by field for this run; `null` turns the platform's off. See [Repair timing after routing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing) |
| `mode` | Default `closure` | `closure` runs the whole flow. `probe` skips the post-CTS and global-route timing repair and stops after global route with its checkpoint; the result is a `PASS` with `routed: false` and `mode: probe`, `xfail:` does not apply to it, and a closure run cannot resume it. A probe cannot be `harden: true`, since it publishes no abstract. See [Probe without timing repair](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#probe-without-timing-repair) |
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
| `floorplan.macros-file` | Optional | YAML or JSON file, relative to `pnr.yaml`, holding a list of `floorplan.macros` entries, applied after the inline ones. Read when the run starts; a missing file or bad entry fails it with `fail_stage: setup`. See [Read macros and pins from planner files](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#read-macros-and-pins-from-planner-files) |
| `floorplan.macro-snap` | Default `true` | `true` snaps each macro `location` to the site and row grid. `false` keeps it exact, rounded once to the database unit |
| `floorplan.pins` | Optional | List of IO pin constraints: ports by name or glob on a side, within a range, as a group, or one pin at an exact location. See below |
| `floorplan.pins-file` | Optional | YAML or JSON file, relative to `pnr.yaml`, holding a list of exact pins (`name`, `location`, optional `layer`, optional `size: [width, height]`, the x and y extents) and `floorplan.pins` entries, applied after the inline ones. Read like `macros-file` |
| `floorplan.pin-snap` | Default `true` | `true` places each exact pin with `place_pin -force_to_die_boundary`, onto the die edge and a routing track. `false` keeps its centre at its location, to the database unit |
| `floorplan.pins-complete` | Default `false` | `true` fails the run before pin placement, with `fail_stage: pins_complete`, unless every port other than power and ground has exactly one exact pin, or no exact pin and exactly one side or group rule. See [Check that every port is placed](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#check-that-every-port-is-placed) |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |
| `xfail_match` | Optional regex | The failure an `xfail`/`xfail_strict` marker expects; a `FAIL` that does not match stays `FAIL`. Requires a marker |

Each `floorplan.blockages` entry has:

- `rect: [x0, y0, x1, y1]` in microns, in die coordinates, non-negative, with `x0 < x1` and `y0 < y1`.
- `type: hard` (default), `soft`, or `partial`.
- For `partial` only, `max-density` strictly between 0 and 1. Only global placement honors it; legalization clears a partial blockage like a hard one.
- `lifetime: flow` (default) or `global-placement`. A `global-placement` blockage exists only while `global_placement` runs: no row is cut under it, macro placement ignores it, and no checkpoint holds it.

Blockages need OpenROAD 26Q1 or later. Macros are kept out of `hard` blockages with the default lifetime, and the rows under them are cut before tap insertion. See [Placement blockages](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#floorplan-controls).

`pdn` has:

- `ring` (optional): `layers: [horizontal, vertical]`, `width`, `spacing`, `offset` in microns. `offset + 2 x width + spacing` must fit between core and die.
- `stripes` (required): each `layer` and `width`, plus either `followpins: true` or a `pitch` with optional `offset` and `spacing`.
- `connect` (optional): `[lower, upper]` layer pairs; default each stripe layer to the next.

It needs a pdn-config (the PDK's or the run's) for the global connections and voltage domain. See [Plan the power grid per run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#plan-the-power-grid-per-run).

Each `floorplan.macros` entry has an `instance` (a full instance name, or a glob over instance names) and at least one of:

- `location: [x, y]`, the lower-left corner of the oriented macro in die microns. Snapped to the site grid unless `macro-snap: false`, it must fit inside the core and clear of other fixed macros. The macro is fixed before macro placement and is a keep-out for the packer. The instance pattern must match exactly one macro.
- `orientation: R0`, `R90`, `R180`, `R270`, `MX`, `MY`, `MXR90` or `MYR90`. Under `macro-placement: rtl-mp` it needs a `location`.
- `halo: [x, y]` in microns, non-negative, replacing `placement.macro-cell-halo` for the matched macros.

A pattern that matches no instance, or a standard cell, fails the run. See [Place individual macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-individual-macros).

Each `floorplan.pins` entry has `names` (port names or globs, a string or a list) and one of:

- `side: left`, `right`, `top` or `bottom`, with optional `start` and `end` in die microns along that edge (`start < end`, both non-negative).
- `group: true`, optionally with `order: true`, alone or with a `side`.
- `location: [x, y]`, the pin centre in die microns, for a single port, with optional `layer` (default: the PDK pin layer of the nearest edge) and `size: [width, height]` in microns, the pin's x and y extents (default: the layer's minimum). `pin-snap` decides whether it moves to the die edge and a track.

A name or glob that matches no port fails the run. A `location` pin is fixed and placed first, before the side and group constraints and the `pin-constraints` file, and side and group constraints leave it out. See [Constrain boundary pins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#constrain-boundary-pins).

The run consumes `<synth dir>/artefacts/<synth>/synth_netlist.v`. The selected PDK and platform provide Liberty, LEF, site, tie and fill cells, CTS buffer, and routing layers.

With `--gds`, KLayout stream-out reads the PDK's `cell-gds` plus the run's `gds-paths`, and is given the technology LEF, the PDK macro LEF, and the run's `lef-paths`. A configured input that is missing stops the export. `gds-mode` decides whether a cell with no layout fails the run or is reported as an incomplete preview. `rb pnr-export` reads the same keys over an already routed result. See [Place and Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-inputs), [Stream-out completeness](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-completeness), and [Export a saved result](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#export-a-saved-result).
