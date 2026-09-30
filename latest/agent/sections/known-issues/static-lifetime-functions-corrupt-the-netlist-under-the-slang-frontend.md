## Static-lifetime functions corrupt the netlist under the slang frontend

A `function` or `task` outside a class declared without `automatic` has one shared storage location per formal, and yosys-slang models this literally. Calls can alias their arguments or leave a net with conflicting drivers, which folds to `x` and can drop a register and everything downstream. Simulation is unaffected, so the defect can go unnoticed.

`rb synth` scans the filelist's sources and included headers before Yosys runs:

- With `frontend: slang`, `static-functions: error` (the default) fails the run. The `verilog` frontend only warns.
- Add `automatic` to the declaration, or set `static-functions: warn` to stage a migration.
- Yosys `multiple conflicting drivers` warnings fail the run unless `conflicting-drivers: allow` is set. Tristate buses are not counted.

The scan reports declarations, so a subroutine with one call site can still fail, and it misses declarations produced by macros or in `-y` directories. A synth.yaml `defines:` that overrides a filelist `+define+` warns `synth.filelist_defines_overridden`. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#gate-static-lifetime-subroutines).
