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
