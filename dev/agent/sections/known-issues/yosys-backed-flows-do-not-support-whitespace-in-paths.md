## Yosys-backed flows do not support whitespace in paths

Yosys script parsing is not shell parsing: whitespace splits tokens, `#` starts a comment, and single quotes from `shlex.quote` do not group a path. Keep design and artifact paths for synthesis and FPV free of whitespace. `fpv.yaml` parameter validation also rejects whitespace, `;`, and `#`.

String-valued parameter overrides require SystemVerilog quotes inside the YAML scalar; ordinary numeric values must not be quoted as strings.
