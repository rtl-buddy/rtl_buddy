## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only `(* blackbox *)` RTL declaration for the frontend.

With both files supplied, the OpenROAD stage keeps the macro's area and timing arcs. Without them rtl_buddy generates a port-only stub, and the reported PPA does not represent the macro.

The declaration can share a SystemVerilog file with other modules and use SystemVerilog in its ports. OpenROAD's `read_verilog` accepts only structural Verilog-2001, so the Yosys stage writes each source file's `(* blackbox *)` modules, and nothing else from the file, as port-only Verilog-2001 modules to `artefacts/<run>/or_<file name>`, and the OpenROAD stage reads that file. A macro with a LEF or Liberty master gets no stub. A blackbox the design does not instance gets none either; Yosys logs `Selection "=<module>" did not match any module`. Widths come from the module as elaborated. A parameterised blackbox has one stub, and an instance that overrides its parameters fails OpenROAD's `read_verilog` unless the module has a LEF and Liberty master; see [Known issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#an-overridden-blackbox-needs-a-lef-and-liberty-master-under-openroad).
