## Troubleshooting

Each entry is a console message and the action it calls for.

- **`static-functions` finding, run fails:** add `automatic` to the listed declarations, or set `static-functions: warn|allow`.
- **`multiple conflicting drivers` warning(s), run fails:** fix the design so each net has one driver style, or set `conflicting-drivers: allow`.
- **Interface instance could not be bound:** use `frontend: slang`, or accept the fallback with `unresolved-interfaces: warn|allow`.
- **multi-clock SDC, abc constraint set to minimum:** use `tool: openroad` for multi-clock timing, or split into one run per clock domain.
- **no `create_clock` found, or `create_clock -period` did not evaluate:** ABC runs unconstrained or skips that clock. Add a literal clock or restore the `tcl` reader.
- **no Tcl interpreter is reachable, or Tcl refused a line:** the tokenizer reader is in use, so `$variables` and `[expr]` stay unevaluated. Install tkinter.
- **`single_unit` or `best_effort_hierarchy` has no effect:** the frontend is not `slang`. Set `frontend: slang` or remove the option.
- **`tool_overrides.yosys` unknown key ignored:** override keys are snake case; the message lists the accepted ones.
- **OpenROAD synthesis requires LEF files:** set `tech-lef` and `macro-lef` on the `cfg-pdks` entry, or `lef-paths` on the run.
- **OpenROAD synthesis requires a mapped library:** set `platform:` on the run and define the matching `cfg-synth-platforms` entry.
- **`phys-model.json` has no per-module breakdown:** Yosys wrote no readable `stat -json`. The run still passes, but `rb phys module` has no rows for it.
- **Previous run's module rows could not be withdrawn:** the run stops rather than leave stale rows over cleared reports. Fix the error the message names (for example a locked or unwritable artefact directory) and rerun.
