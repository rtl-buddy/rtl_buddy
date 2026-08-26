## Supported backends

`rb synth` ships two backends selectable via `tool:` in `synth.yaml`. Both backends use Yosys to map RTL to a gate-level netlist; they differ in whether OpenROAD is run afterwards for static timing analysis.

| `tool:` | Backend | Multi-clock SDC | Reports |
|---------|---------|-----------------|---------|
| `yosys` | Yosys + ABC (single stage) | Workaround (min period) | Gates, Area, WNS |
| `openroad` | Yosys (stage 1) + OpenROAD STA (stage 2) | Native `read_sdc` | Gates, Area, WNS, TNS |

The `openroad` backend removes the multi-clock SDC workaround: stage 1 maps RTL to a gate-level netlist with Yosys, stage 2 feeds that netlist into OpenROAD which loads the SDC natively and reports WNS (actual worst slack from `report_checks`) and TNS (total negative slack).
