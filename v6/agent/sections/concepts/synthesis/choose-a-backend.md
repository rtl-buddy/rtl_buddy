## Choose a backend

| `tool:` | Flow | Clock handling | Results |
| --- | --- | --- | --- |
| `yosys` | Yosys and ABC | Uses the minimum SDC clock period | Gates, area, WNS |
| `openroad` | Yosys mapping, then OpenROAD STA | Reads the full multi-clock SDC | Gates, area, WNS, TNS |

Use `yosys` for technology-independent synthesis or a quick mapped result. Use `openroad` when timing must respect multiple clocks or you need OpenROAD STA.

Both backends use Yosys for RTL elaboration and mapping. The OpenROAD backend adds a second stage over the mapped netlist.
