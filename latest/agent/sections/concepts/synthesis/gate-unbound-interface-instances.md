## Gate unbound interface instances

`read_verilog` cannot bind a SystemVerilog interface *instance* to the
interface port of a child module. Instead of failing, it derives a per-child
`<child>$interfaces$<interface>` module whose ports are the interface's
members and wires them in the parent through implicitly declared
`<instance>.<member>` wires, reporting one
``Could not find interface instance for `<instance>' in `<module>'`` warning
and exiting 0.

The members usually survive that fallback. The interface instance's **own port
connections do not**:

```systemverilog
interface bus_if (input logic clk);
  logic [7:0] data;
  logic       vld;
  modport src (input clk, output data, vld);
  modport dst (input clk, input  data, vld);
endinterface

module top (input logic clk, ...);
  bus_if b (.clk(clk));          // .clk is dropped
  producer u_p (.b(b), ...);     // both children clock off an
  consumer u_c (.b(b), ...);     // undriven \b.clk
endmodule
```

`\b.data` and `\b.vld` still connect `u_p` to `u_c`, but `\b.clk` is left
undriven and `top`'s `clk` input goes unused — every flop in the subtree loses
its clock, with a warning and an exit code of 0. `frontend: slang` binds the
instance properly and emits neither the warning nor the disconnect.

`unresolved-interfaces` gates the warning. It defaults to `warn`, which logs
one `synth.unresolved_interface` per instance, records `unresolved_interfaces`
in the result envelope, and lets the run pass: the fallback *is* correct for an
interface with no ports of its own, or one whose ports nothing in the subtree
reads, and erroring by default would fail those designs. Set `error` in a project that uses interface ports and wants the
hazard gated — a failed run drops the netlist so `rb pnr` and `rb power`
cannot consume it — or `allow` to silence the warning once it is understood.
