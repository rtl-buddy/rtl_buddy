## Choose the SystemVerilog frontend

Use the simplest frontend that elaborates the property set:

| Frontend | Use when | Requirements and limits |
|---|---|---|
| `verilog` | Immediate assertions and simple concurrent assertions | Built into Yosys; no plugin. Does not support implications, sequence operators, or compilation-unit `bind` correctly |
| `slang` | `bind`, `|->`, `|=>`, sequences, or richer SystemVerilog | `yosys-slang` plugin configured by `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN` |

With `frontend: verilog`, a listed property set that elaborates no assert, assume, or cover cells fails instead of reporting a vacuous PASS. When using slang for concurrent SVA, use a build that lowers the constructs you need; the [rtl-buddy yosys-slang branch](https://github.com/rtl-buddy/yosys-slang/tree/rtl-buddy) supports the rtl_buddy property flow. Probe your installed build before authoring a large property set because accepted sampled-value functions and sequence constructs vary by build.

```systemverilog
module probe(input logic clk, a, b);
  a1: assert property (@(posedge clk) a |-> b);
  a2: assert property (@(posedge clk) $past(a) |-> b);
  a3: cover property (@(posedge clk) a && b);
endmodule
```

Test one uncertain construct at a time; one rejected construct aborts the whole read.
