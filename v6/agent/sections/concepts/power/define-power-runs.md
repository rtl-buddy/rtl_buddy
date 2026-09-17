## Define power runs

```yaml
rtl-buddy-filetype: power_config

runs:
  - name: demo_power_static
    desc: Static post-synthesis power
    tool: openroad
    mode: static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    reglvl: 1000

  - name: demo_power_saif
    desc: Simulation-driven post-route power
    tool: openroad
    mode: dynamic
    netlist-source: pnr
    pnr: demo_pnr_nangate45
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: nangate45_typ
    activity:
      saif: ../../verif/demo/artefacts/csr_smoke/dump.saif
      scope: tb_top/u_dut
    reglvl: 1000
```

Paths resolve from `power.yaml`. A synth-source run requires `synth`, `synth-path`, and `constraints`. A P&R-source run requires `pnr` and `pnr-path`; it uses the routed SDC unless `constraints` overrides it. `phys-run` is optional and names the synthesis run this one publishes its half of the physical model beside; see [Pair the model with a synthesis run](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/#pair-the-model-with-a-synthesis-run).

See [YAML Formats: power.yaml](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#poweryaml) for all fields.
