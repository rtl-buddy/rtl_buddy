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
    reglvl: 1000
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `tool` | Default `openroad` | Backend |
| `synth` | Required | Upstream synthesis entry |
| `synth-path` | Required | Upstream `synth.yaml`, relative to `pnr.yaml` |
| `constraints` | Required | SDC path relative to `pnr.yaml` |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `desc` | Required | Human-readable description |
| `lef-paths` / `lib-paths` | Optional | Design-specific macro files relative to `pnr.yaml` |
| `floorplan.utilization` | Default 0.55 | Core utilization from 0 to 1 |
| `floorplan.aspect` | Default 1.0 | Die aspect ratio |
| `floorplan.core-margin` | Default 2.0 | Core-to-die margin in microns |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

The run consumes `<synth dir>/artefacts/<synth>/synth_netlist.v`. The selected PDK and platform provide Liberty, LEF, site, tie/fill cells, CTS buffer, and routing layers. See [Place and Route](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/).
