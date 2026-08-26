## power.yaml

**Required keys:**

- `rtl-buddy-filetype: power_config`
- `runs`

**Example:**

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

  - name: "demo_power_postpnr"
    desc: "Post-PnR power from the routed ODB"
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

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Run identifier; used on the CLI and in `artefacts/<name>/` |
| `desc` | string | Human-readable description (required — no default) |
| `tool` | string | Backend tool name; default `"openroad"` (the only backend today) |
| `mode` | string | `"static"` (default) or `"dynamic"`. Static skips activity; dynamic applies an activity source |
| `netlist-source` | string | `"synth"` (default) or `"pnr"`. Selects post-synth netlist vs post-PnR routed ODB |
| `synth` | string | Upstream `rb synth` entry name — **required when** `netlist-source: synth` |
| `synth-path` | string | Path to the `synth.yaml`, relative to `power.yaml` — required when `netlist-source: synth` |
| `pnr` | string | Upstream `rb pnr` entry name — **required when** `netlist-source: pnr` |
| `pnr-path` | string | Path to the `pnr.yaml`, relative to `power.yaml` — required when `netlist-source: pnr` |
| `constraints` | string | SDC path (required for `synth` source; optional for `pnr` source — defaults to the post-CTS `<top>.routed.sdc`) |
| `platform` | string | `cfg-pnr-platforms` entry name — reused for Liberty + corner |
| `activity.saif` | string | Path to a SAIF v2 file (mutually exclusive with `vcd`) |
| `activity.vcd` | string | Path to a VCD trace (mutually exclusive with `saif`) |
| `activity.scope` | string | Hierarchical scope for OpenROAD's `-scope`. Only valid alongside `saif`/`vcd`; set without a trace it raises a config-load error |
| `activity.default-toggle-rate` | float | Synthetic global toggle rate (used in `dynamic` mode with no trace). Default `0.1` |
| `activity.default-static-prob` | float | Synthetic global duty cycle. Default `0.5` |
| `reglvl` | int or dict | Regression level for filtering; same semantics as `synth.yaml`/`pnr.yaml` reglvl |
| `tool_overrides` | dict | Reserved for tool-specific overrides; accepted but not consumed by the OpenROAD backend today (mirrors `pnr.yaml`) |

**Runtime effects:**

- `rb power` resolves the netlist per `netlist-source`: `synth` reads `synth_netlist.v` from the upstream `rb synth` run (`read_verilog`); `pnr` reads `<top>.routed.odb` from the upstream `rb pnr` run (`read_db`) and runs `estimate_parasitics -global_routing` for routing-derived wire-cap (no SPEF). See the [Power Analysis concept page](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/power/) for the full activity-source matrix.
- The resolved activity source (`default` / `synthetic` / `saif` / `vcd`) is decided at config load and surfaced in the results table.
- Pass when `openroad` exits 0, the log has no `[ERROR ...]` lines, and the `Total` line in `power.rpt` parses. SKIP when the entry's `reglvl` is above `--reg-level` or `tool:` is not in the backend registry.

---
