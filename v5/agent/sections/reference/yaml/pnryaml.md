## pnr.yaml

**Required keys:**

- `rtl-buddy-filetype: pnr_config`
- `runs`

**Example:**

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

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | P&R run identifier; used on the CLI and in `artefacts/<name>/` |
| `desc` | string | Human-readable description |
| `tool` | string | Backend tool — `"openroad"` is the only supported value today |
| `synth` | string | Name of the upstream `rb synth` entry that produced the netlist |
| `synth-path` | string | Path to the `synth.yaml` that defines `synth`, resolved relative to `pnr.yaml` |
| `constraints` | string | Path to the SDC file (required), resolved relative to `pnr.yaml` |
| `platform` | string | `cfg-pnr-platforms` entry name |
| `lef-paths` | list of strings | Optional design-specific macro LEF files (e.g. SRAM macros), resolved relative to `pnr.yaml`; emitted as extra `read_lef` lines after the platform's tech/macro LEF |
| `lib-paths` | list of strings | Optional design-specific macro Liberty files, resolved relative to `pnr.yaml`; emitted as extra `read_liberty` lines |
| `floorplan.utilization` | float | Core utilization (0–1) |
| `floorplan.aspect` | float | Die aspect ratio |
| `floorplan.core-margin` | float | Margin between core area and die edge, in microns |
| `reglvl` | int or dict | Regression level for filtering; same semantics as `synth.yaml` reglvl (int for all tools, dict for per-tool with `default`) |
| `tool_overrides` | dict | Reserved for tool-specific overrides (none consumed today) |

**Runtime effects:**

- `rb pnr` loads `pnr.yaml`, resolves the upstream `synth-path` + `synth` to find `<synth_dir>/artefacts/<synth_name>/synth_netlist.v`, and dispatches to the OpenROAD backend.
- The backend writes `pnr.tcl` from a bundled template, invokes `openroad -no_init -exit -log artefacts/<name>/pnr.log artefacts/<name>/pnr.tcl`, and produces routed DEF + post-route netlist/SDC + timing/DRC reports under `artefacts/<name>/`.
- The selected `cfg-pnr-platforms` entry provides Liberty, tech-LEF, macro-LEF, SITE, tie cells, fill cells, CTS buffer, and routing layer ranges via its referenced `cfg-pdks` entry.
- Pass when OpenROAD exits 0 and the log has no `[ERROR ...]` lines. SKIP when the entry's `reglvl` is above `--reg-level` or `tool:` is not `openroad`.

---
