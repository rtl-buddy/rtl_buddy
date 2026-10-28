## pnr.yaml

Required keys are `rtl-buddy-filetype: pnr_config` and `runs`. Paths are relative to `pnr.yaml`. How to use the fields is in [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/).

An unknown key in a run or any mapping inside it is a config error naming the file, run, position, the nearest valid key and every valid key, for example `pnr run 'top': floorplan.blockages[2]: unknown key 'lifetme', did you mean 'lifetime'?`. All unknown keys are reported at once.

```yaml
rtl-buddy-filetype: pnr_config
runs:
  - name: demo_pnr
    desc: OpenROAD place and route
    synth: demo_synth
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan: {utilization: 0.55, aspect: 1.0, core-margin: 2.0}
    lef-paths: [../../pdk/sram/sram.lef]
    gds-paths: [../../pdk/sram/sram.gds]
    reglvl: 1000
```

| Field | Default | Meaning |
|---|---|---|
| `name` | Required | Run name and artefact directory |
| `desc` | Required | Description |
| `synth`, `synth-path` | Required | Upstream synthesis entry and its `synth.yaml`. The run reads `artefacts/<synth>/synth_netlist.v` |
| `constraints` | Required | SDC file, or on a multi-corner platform a map of each corner to its SDC file. A map that misses a corner or names another fails setup. See [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners) |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `tool` | `openroad` | Only `openroad` runs; anything else is `SKIP` |
| `lef-paths` | — | Extra macro LEF |
| `lib-paths` | — | Extra macro Liberty. Each entry is a path, timed at every corner, or a map of each platform corner to its Liberty. A map that misses a corner or names another fails setup, as does a file listed both as a plain path and in a map. See [Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners) |
| `gds-paths` | — | Macro layouts for stream-out |
| `gds-mode` | `preview` | `strict` fails the run when a requested export is incomplete. `--gds-mode` overrides. See [Stream out GDS and PNG](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-gds-and-png) |
| `gds-allow-empty` | — | Cell names or case-sensitive globs that have no layout on purpose |
| `pin-constraints` | — | Tcl sourced just before pin placement |
| `hooks` | — | Map of hook point to a Tcl path or list. Points, in flow order: `session`, `after-floorplan`, `macro-placement`, `after-pdn`, `after-pins`, `after-global-placement`, `after-repair-design`, `before-cts`, `before-repair-clock-nets`, `after-cts`, `after-cts-repair`, `after-global-route`, `after-global-route-repair`, `before-detailed-route`, `after-extraction`, `before-reports`, `after-reports`, and `stage-end` at the end of every stage. See [Run Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks) |
| `inputs` | — | Files the SDC or hooks `source`, relative to `pnr.yaml`. Fingerprinted for `--resume` and hardened-block staleness. See [Run Tcl hooks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-tcl-hooks) |
| `pdn-config` | PDK's | Tcl that replaces the PDK's `pdn-config` |
| `pdn` | — | Core grid in YAML; see below |
| `placement` | Platform's | Any `placement.*` key, overriding the platform for this run |
| `routing-layers`, `routing-layer-adjustment`, `cts-buffer`, `cts-root-buffer`, `cts-clock-nets`, `cts-latency-balancing`, `cts-repair-clock-nets`, `cts-sink-clustering`, `cts-obstruction-aware`, `cts-dont-use-cells`, `post-cts-setup-repair`, `global-route-hold-repair`, `port-buffer`, `keep-sizing-vt`, `tapcell-tcl` | Platform's | Override the platform's value for this run. See [P&R keys](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#synthesis-and-physical-design-tools) |
| `global-route` | Platform's | Overrides the platform's block field by field. See [P&R keys](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#synthesis-and-physical-design-tools) |
| `global-route-repair`, `post-route-repair` | Platform's | Overrides the platform's block field by field; `null` turns it off. See [Repair timing after routing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#repair-timing-after-routing) |
| `mode` | `closure` | `probe` skips timing repair and stops after global route. Not with `harden: true`. See [Probe a floorplan](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#probe-a-floorplan) |
| `signoff` | Platform's | Overrides the platform's block field by field; `null` turns it off. See [Gate signoff](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#gate-signoff) |
| `fail-on-electrical` | `false` | `true` fails a routed run with any max-slew, max-capacitance or max-fanout violator, and post-route repair counts each round's max-slew and max-capacitance violators, so a round that ties on timing and DRCs wins with fewer. It adds `electrical` to `signoff.fail-on` |
| `threads` | 1 | Positive integer, or `auto` for the CPUs of the current allocation. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads) |
| `detailed-route-verbose` | 1 | `detailed_route -verbose` level; 0 silences the iteration log |
| `checkpoints` | `false` | `true`, a stage, or a list of `floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `post_route_repair`. `[]` writes only the progress log. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#keep-stage-checkpoints) |
| `stages` | `false` | `true` runs each stage in its own session. A `checkpoints` list may then leave out only `post_route_repair`. See [Run stages as separate sessions](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-stages-as-separate-sessions) |
| `resources` | — | `cpus`, `mem`, quoted `time`, and `stages: {<stage>: {...}}`, for `--dispatch slurm` only. See [Dispatch the stages to Slurm](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm) |
| `harden` | `false` | Publish the routed block as an abstract. Needs a single-corner platform. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block) |
| `abstract` | — | Needs `harden`. `bloat`: `occupied` (default) or a number; `feedthrough-layers`: layers written with real shapes; `gds: false`: publish no GDS and skip the forced stream-out; a parent's `strict` stream-out then fails at setup and a `preview` one lacks the block |
| `buffer-ports` | Value of `harden` | `true` buffers every input and output port after pin placement, `false` none. A mapping chooses: `inputs` and `outputs` (default `true`) add `buffer_ports -inputs` and `-outputs`, and at least one must be on; `skip-clock-outputs: true` leaves each output that carries a clock (on the clock network, or a generated clock's source) unbuffered; `hold-outputs-at-pins: true` fixes each output buffer on the free row site nearest its pin through global placement. Both default `false` and need `outputs`. See [Check and repair slew at output ports](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#check-and-repair-slew-at-output-ports) |
| `blocks` | — | Hardened blocks: `name` (module), `pnr` (a `harden: true` run), `pnr-path` (default this file). Needs a single-corner platform. See [Assemble hardened blocks](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#assemble-hardened-blocks) |
| `floorplan.utilization` | 0.55 | Fraction of the core area the cells fill |
| `floorplan.aspect` | 1.0 | Die aspect ratio |
| `floorplan.core-margin` | 2.0 | Core-to-die margin in µm |
| `floorplan.die-area`, `floorplan.core-area` | — | `[x0, y0, x1, y1]` in µm, set together, core inside die. Exclude the three keys above |
| `floorplan.core-cutouts` | — | Rectangles removed from the core |
| `floorplan.blockages` | — | Placement blockages; see below |
| `floorplan.macro-placement` | `pack` | `pack` (rb's packer) or `rtl-mp` (OpenROAD's RTL-MP) |
| `floorplan.macro-anchor` | `lower-left` | Core corner the packer starts from. Another corner is an error with `rtl-mp` or a `macro-placement` hook |
| `floorplan.macros`, `floorplan.macros-file` | — | Per-macro directives, inline or from a YAML or JSON file; see below |
| `floorplan.macro-snap` | `true` | `false` keeps macro locations off the site and row grid |
| `floorplan.pins`, `floorplan.pins-file` | — | Pin constraints, inline or from a YAML or JSON file; see below |
| `floorplan.pin-snap` | `true` | `false` keeps exact pin centres instead of moving them to the die edge and a track |
| `floorplan.pins-complete` | `false` | `true` fails the run when a signal port has no entry, more than one exact pin, or no exact pin and several side or group rules |
| `reglvl` | 0 | Regression level: an integer, or a map of tool name to level with an optional `default` |
| `xfail`, `xfail_strict`, `xfail_match` | — | [Expected failures](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/expected-failures/) |
| `tool_overrides` | — | Accepted, unused |

Each `floorplan.blockages` entry:

- `rect: [x0, y0, x1, y1]` in die µm.
- `type`: `hard` (default; rows cut, macros kept out), `soft` (global placement only) or `partial` with `max-density` between 0 and 1.
- `lifetime`: `flow` (default) or `global-placement`, which exists only during global placement.

Blockages need OpenROAD 26Q1.

Each `floorplan.macros` entry has `instance` (a name, or a glob where `*` crosses `/`) and at least one of:

- `location: [x, y]`: lower-left corner after orientation, die µm. Must match one macro.
- `orientation`: `R0`, `R90`, `R180`, `R270`, `MX`, `MY`, `MXR90` or `MYR90`. Under `rtl-mp` it needs a `location`.
- `halo: [x, y]`: standard-cell keep-out in µm, replacing `placement.macro-cell-halo`.

Only one entry may set each key for a macro.

Each `floorplan.pins` entry has `names` (a name or glob, or a list) and one of:

- `side`: `left`, `right`, `top` or `bottom`, with optional `start` and `end` in die µm along that edge.
- `group: true`, with optional `order: true`, alone or with a `side`.
- `location: [x, y]`: the centre of one pin in die µm, with optional `layer` and `size: [width, height]`.

A `pins-file` entry is a `floorplan.pins` entry or an exact pin with `name`, `location`, optional `layer` and `size`. File entries apply after the inline ones.

`pdn`:

- `ring` (optional): `layers: [horizontal, vertical]`, `width`, `spacing`, `offset` in µm. It must fit between core and die.
- `stripes`: each has `layer` and `width`, plus `followpins: true` or a `pitch` with optional `offset` and `spacing`.
- `connect` (optional): `[lower, upper]` layer pairs. Default: each stripe layer to the next.

It replaces the core grid of the pdn-config in effect, which must exist.
