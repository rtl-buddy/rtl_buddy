## Constraints

Each `design.constraints` entry names an SDC file and the preserved module it constrains (`scope`). The file is evaluated in the constraint reader's Tcl interpreter, so variables and loops are expanded, and written back out with:

- each `get_ports` pattern checked against the scope module's ports, failing on a pattern that matches none;
- each `get_cells` / `get_pins` / `get_nets` path translated segment by segment through the name map, including `_reg` and bit-select suffixes;
- `get_clocks` and the `all_*` queries passed through.

A wildcard that would have to match renamed names, `-filter`, and `source` cannot be translated faithfully and fail the release; list the objects explicitly. The rewritten files ship under `design/constraints/`.

A constraint script that queries the design while it runs (`get_property`, loops over `all_inputs`) cannot be evaluated without a netlist. Give it `mode: verbatim`: it ships unchanged after a check that it names no internal objects (`get_cells`, `get_pins`, `get_nets`) and that each literal `get_ports` pattern matches a port of its scope. Patterns built from variables are not checked.
