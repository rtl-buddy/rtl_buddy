## `toplevel:` must name the testbench in a plain SystemVerilog test

For a plain SystemVerilog testbench, `toplevel:` becomes Verilator `--top-module`, VCS `-top` or Icarus `-s`. It must name the testbench, not the DUT. cocotb and SystemC testbenches need no review.

- A `toplevel:` naming the DUT compiles, then the simulation exits at once and the test ends `NA` with `no PASS/FAIL markers found in .../test.log; result is NA`. Nothing in that output names `toplevel:`. A DUT with unconnected interface ports fails with `%Error-UNSUPPORTED: Interfaced port on top level module` instead.
- Without `toplevel:`, the top is chosen from filelist order, not the testbench `name`.
- A top pinned in the builder's `compile-time` opts overrides `toplevel:`. When they differ, the run warns `compile.toplevel_conflict` and uses the pinned top.
