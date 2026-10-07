## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only `(* blackbox *)` RTL declaration for the frontend.

With both files supplied, the OpenROAD stage keeps the macro's area and timing arcs. Without them rtl_buddy generates a port-only stub, and the reported PPA does not represent the macro.
