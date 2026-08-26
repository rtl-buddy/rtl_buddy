## Supported backends

`rtl_buddy` ships two synthesis backends selectable via `tool:` in `synth.yaml`:

| `tool:` | Backend | Multi-clock SDC | Reports |
|---------|---------|-----------------|---------|
| `yosys` | Yosys + ABC | Workaround (min period) | Gates, Area, WNS |
| `openroad` | Yosys (stage 1) + OpenROAD STA (stage 2) | Native `read_sdc` | Gates, Area, WNS, TNS |

The OpenROAD backend removes the multi-clock SDC workaround: stage 1 maps RTL to a gate-level netlist with Yosys, stage 2 feeds that netlist into OpenROAD which loads the SDC natively and reports WNS (actual worst slack from `report_checks`) and TNS (total negative slack).
