## Define a P&R run

```yaml
rtl-buddy-filetype: pnr_config

runs:
  - name: demo_pnr_nangate45
    desc: Nangate45 typical-corner P&R
    tool: openroad
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    floorplan:
      utilization: 0.55
      aspect: 1.0
      core-margin: 2.0
    reglvl: 1000
```

Paths resolve from `pnr.yaml`. The named synthesis must already have produced `artefacts/<synth>/synth_netlist.v`. RTL Buddy takes the top module from that synthesis entry and takes Liberty and LEF assets from the selected physical platform.

Only `tool: openroad` is supported. Other tool names report `SKIP`. See [YAML Formats: pnr.yaml](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#pnryaml) for all fields.
