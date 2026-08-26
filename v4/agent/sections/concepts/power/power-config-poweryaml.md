## Power config: `power.yaml`

```yaml
rtl-buddy-filetype: power_config

runs:
  - name: "demo_power_static"
    desc: "Static power on Nangate45 typ corner"
    tool: "openroad"
    mode: "static"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    reglvl: 1000

  - name: "demo_power_dynamic_synthetic"
    desc: "Dynamic power with synthetic global activity"
    tool: "openroad"
    mode: "dynamic"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    activity:
      default-toggle-rate: 0.2
      default-static-prob: 0.5
    reglvl: 1000

  - name: "demo_power_dynamic_saif"
    desc: "Dynamic power driven by simulation SAIF"
    tool: "openroad"
    mode: "dynamic"
    synth: "demo_synth_nangate45"
    synth-path: "../../synth/demo/synth.yaml"
    constraints: "../../synth/demo/constraints.sdc"
    platform: "nangate45_typ"
    activity:
      saif: "../../verif/demo/artefacts/csr_smoke/dump.saif"
      scope: "tb_top/u_dut"
    reglvl: 1000

  - name: "demo_power_postpnr"
    desc: "Post-PnR power with SPEF parasitics"
    tool: "openroad"
    mode: "dynamic"
    netlist-source: "pnr"
    pnr: "demo_pnr_nangate45"
    pnr-path: "../../pnr/demo/pnr.yaml"
    platform: "nangate45_typ"
    activity:
      saif: "../../verif/demo/artefacts/csr_smoke/dump.saif"
      scope: "tb_top/u_dut"
    reglvl: 1000
```

### Fields

| Field | Description |
|---|---|
| `name` | Run identifier; used on the command line and in `artefacts/<name>/` |
| `desc` | Human-readable description |
| `tool` | Backend tool name — only `openroad` is supported today |
| `mode` | `"static"` or `"dynamic"`. Static skips activity entirely; dynamic applies one of the activity sources below |
| `netlist-source` | `"synth"` (default) or `"pnr"`. Selects post-synth vs post-PnR netlist |
| `synth` | Name of the upstream `rb synth` entry — **required when** `netlist-source: synth` |
| `synth-path` | Path to the `synth.yaml`, resolved relative to `power.yaml` — required when `netlist-source: synth` |
| `pnr` | Name of the upstream `rb pnr` entry — **required when** `netlist-source: pnr` |
| `pnr-path` | Path to the `pnr.yaml`, resolved relative to `power.yaml` — required when `netlist-source: pnr` |
| `constraints` | SDC path (required for `synth` source; optional for `pnr` source — defaults to `routed.sdc`) |
| `platform` | `cfg-pnr-platforms` entry name — reused for Liberty + corner |
| `activity.saif` | Path to a SAIF v2 file (mutually exclusive with `vcd`) |
| `activity.vcd` | Path to a VCD trace (mutually exclusive with `saif`) |
| `activity.scope` | Hierarchical scope passed to OpenROAD's `-scope` flag when reading a trace. Only valid alongside `saif`/`vcd` — set without a trace, it raises a config-load error |
| `activity.default-toggle-rate` | Synthetic global activity rate (used when `mode: dynamic` and no trace is supplied). Default `0.1` |
| `activity.default-static-prob` | Synthetic global duty cycle. Default `0.5` |
| `reglvl` | Regression level for filtering (same semantics as `rb synth` / `rb pnr`) |

### Activity source resolution

Decision happens at config load (`PowerConfig.get_activity_source()`), not in the backend, so every backend agrees on the strategy:

| `mode` | `activity.saif` | `activity.vcd` | → resolved source |
|---|---|---|---|
| `static` | (ignored) | (ignored) | `default` — no activity command emitted |
| `dynamic` | set | — | `saif` — backend emits `read_saif` |
| `dynamic` | — | set | `vcd` — backend emits `read_power_activities -vcd` |
| `dynamic` | — | — | `synthetic` — backend emits `set_power_activity -global` |

The resolved source is recorded in the results table as the `Activity` column.
