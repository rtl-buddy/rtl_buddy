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
| `gds-mode` | Default `preview` | `strict` fails the run when a requested export is not delivered complete; `preview` keeps an incomplete layout and reports it. `--gds-mode` overrides |
| `gds-allow-empty` | Optional | Cell names or `fnmatch` globs that are empty on purpose, matched case-sensitively. Such a cell is not missing in either mode |
| `threads` | Default unset (1) | OpenROAD worker threads: a positive integer, or `auto` for the CPUs of the current allocation (1 outside one). Clamped to a detected Slurm or affinity allocation with a warning. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#openroad-threads) |
| `checkpoints` | Default `false` | `true`, a stage name, or a list of `floorplan`, `place`, `cts`, `global_route`: write a stage-named ODB, DEF and SDC (plus route guides and segments after `global_route`) under `artefacts/<run>/checkpoints/<run-id>/`, with a manifest and a `progress.jsonl` of step events. `[]` keeps progress only. Unset renders the flow unchanged. See [Keep stage checkpoints](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#keep-stage-checkpoints) |
| `floorplan.utilization` | Default 0.55 | Core utilization from 0 to 1 |
| `floorplan.aspect` | Default 1.0 | Die aspect ratio |
| `floorplan.core-margin` | Default 2.0 | Core-to-die margin in microns |
| `floorplan.macro-anchor` | Default `lower-left` | Core corner the macro packer starts from: `lower-left`, `lower-right`, `upper-left` or `upper-right`. See [Floorplan controls](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#floorplan-controls) |
| `floorplan.blockages` | Optional | List of standard-cell placement blockages. Each is `rect: [x0, y0, x1, y1]` in microns, die coordinates (`x0 < x1`, `y0 < y1`, non-negative), `type: hard` (default), `soft` or `partial`, and for `partial` only, `max-density` strictly between 0 and 1 (honoured by global placement only; legalization clears a partial blockage like a hard one). Needs OpenROAD 26Q1+. Macros are kept out of `hard` blockages |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

The run consumes `<synth dir>/artefacts/<synth>/synth_netlist.v`. The selected PDK and platform provide Liberty, LEF, site, tie/fill cells, CTS buffer, and routing layers. With `--gds`, KLayout stream-out reads the PDK's `cell-gds` plus the run's `gds-paths`, and is given the technology LEF, the PDK macro LEF and the run's `lef-paths`; a configured input that is missing stops the export. `gds-mode` decides whether a cell with no layout at all fails the run or is reported as an incomplete preview. `rb pnr-export` reads the same keys over a result that is already routed, without running P&R. See [Place and Route](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#stream-out-inputs), [Stream-out completeness](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#stream-out-completeness) and [Export a saved result](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#export-a-saved-result).
