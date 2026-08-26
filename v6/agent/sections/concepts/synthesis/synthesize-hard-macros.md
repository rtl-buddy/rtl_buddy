## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only RTL `(* blackbox *)` declaration for frontend binding.

The OpenROAD stage avoids generating a Verilog stub when the macro already exists in the supplied LEF or Liberty, preserving its physical area and timing arcs. If no physical or timing master exists, RTL Buddy generates a port-only stub and the reported PPA cannot represent that macro accurately.
