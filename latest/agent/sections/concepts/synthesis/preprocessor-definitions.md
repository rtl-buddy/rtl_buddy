## Preprocessor definitions

The scan and the frontend see the filelist's `+define+` entries, then the run's `defines:`, then the frontend's own macros. `read_verilog` predefines `SYNTHESIS` and `YOSYS`; `read_slang` predefines `SYNTHESIS` but not `YOSYS`. An `` `ifndef YOSYS `` helper is therefore reported only under `frontend: slang`.

- Write `+define+X=1` when a value is meant. A bare `+define+X` gets the value the frontend gives a valueless `-D`, which differs between tools.
- When `defines:` overrides a filelist entry with a different value, or overrides a bare entry, the run warns with `synth.filelist_defines_overridden`. Simulation then uses the filelist value and synthesis the `synth.yaml` one. Drop one of the two to make them agree.
- A filelist `+define+` value containing whitespace is fatal.
