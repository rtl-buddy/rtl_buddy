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
| `phys-run` | Optional, synth source only | Synthesis run in `synth-path` whose artefact directory this run publishes `phys-model.json` into |
| `constraints` | Required for synth source | SDC path; for P&R source defaults to routed SDC |
| `platform` | Required | `cfg-pnr-platforms` entry |
| `lib-paths` | Optional | Extra macro Liberty, relative to `power.yaml`, appended after what the referenced run declares |
| `threads` | Default unset (1) | OpenROAD worker threads, as in `pnr.yaml`. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads) |
| `activity.saif` / `.vcd` | Mutually exclusive | Activity trace path |
| `activity.scope` | Only with a trace | OpenROAD trace scope; invalid without SAIF/VCD |
| `activity.default-toggle-rate` | Default 0.1 | Synthetic toggle rate for dynamic mode without a trace |
| `activity.default-static-prob` | Default 0.5 | Synthetic static probability |
| `reglvl` | Optional | Regression level |
| `tool_overrides` | Accepted, unused | Reserved per-tool mapping |
| `xfail` / `xfail_strict` | Default false | Expected-failure handling |

Hard-macro Liberty is inherited from the run this one reads: a `pnr` source takes the P&R entry's `lib-paths`, a `synth` source takes the synthesis entry's `lib-paths` and `lef-paths`. `lib-paths` here adds to that list rather than replacing it, and a configured file that is not on disk fails the run before OpenROAD starts. See [Give hard macros a library](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#give-hard-macros-a-library).

P&R source reads the routed ODB and estimates parasitics from global routing; synthesis source reads the generated netlist. Without `phys-run` the physical model is published into this run's own `artefacts/<name>/`, so it merges with a synthesis' half only when both runs write there; `phys-run` names the synthesis run to publish beside instead and is a run name, never a path. See [Power Analysis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/) and [Pair the model with a synthesis run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#pair-the-model-with-a-synthesis-run).
