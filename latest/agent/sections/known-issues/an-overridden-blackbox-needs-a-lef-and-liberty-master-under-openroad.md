## An overridden blackbox needs a LEF and Liberty master under OpenROAD

The OpenROAD synthesis backend passes a `(* blackbox *)` module that has no LEF or Liberty master to OpenROAD as a port-only stub. An instance that overrides its parameters keeps its `#(...)` list, which OpenROAD's `read_verilog` rejects (`STA-0171`). Give the module a LEF and Liberty, as `blocks:` does for a hardened block, or instance it with its defaults. See [Instance a parameterised block](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#instance-a-parameterised-block).
