## Yosys-backed flows do not support whitespace in paths

Yosys scripts split on whitespace and treat `#` as a comment, and quoting does not group a path. Keep design and artifact paths for synthesis and FPV free of whitespace. `fpv.yaml` parameter validation also rejects whitespace, `;` and `#`. String-valued parameter overrides need SystemVerilog quotes inside the YAML scalar.
