## Choose a backend

| `tool:` | Flow | Clock handling | Results |
| --- | --- | --- | --- |
| `yosys` | Yosys and ABC | Uses the shortest SDC clock period | Gates, area, WNS |
| `openroad` | Yosys mapping, then OpenROAD STA | Reads the full multi-clock SDC | Gates, area, WNS, TNS |

Use `yosys` for technology-independent synthesis or a quick mapped result. Use `openroad` when timing must respect multiple clocks: a Yosys run passes only the shortest `create_clock` period to ABC and warns when there are several.
