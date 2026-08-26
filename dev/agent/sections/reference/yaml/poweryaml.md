## power.yaml

Required keys are `rtl-buddy-filetype: power_config` and `runs`.

```yaml
rtl-buddy-filetype: power_config
runs:
  - name: demo_power
    desc: Post-route dynamic power
    tool: openroad
    mode: dynamic
    netlist-source: pnr
    pnr: demo_pnr
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: nangate45_typ
    activity:
      saif: ../../verif/demo/artefacts/smoke/dump.saif
      scope: tb_top/u_dut
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Run identifier and artefact directory |
| `desc` | Required | Human-readable description |
| `tool` | Default `openroad` | Backend |
| `mode` | Default `static` | `static` or `dynamic` |
| `netlist-source` | Default `synth` | `synth` or `pnr` |
| `synth`, `synth-path` | Required for synth source | Upstream synthesis entry and YAML path |
| `pnr`, `pnr-path` | Required for P&R source | Upstream P&R entry and YAML path |
| `constraints` | Required for synth source | SDC path; for P&R source defaults to routed SDC |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `activity.saif` / `.vcd` | Mutually exclusive | Activity trace path |
| `activity.scope` | Only with a trace | OpenROAD trace scope; invalid without SAIF/VCD |
| `activity.default-toggle-rate` | Default 0.1 | Synthetic toggle rate for dynamic mode without a trace |
| `activity.default-static-prob` | Default 0.5 | Synthetic static probability |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

P&R source reads the routed ODB and estimates parasitics from global routing; synthesis source reads the generated netlist. See [Power Analysis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/).
