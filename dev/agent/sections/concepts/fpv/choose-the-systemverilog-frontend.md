## Choose the SystemVerilog frontend

Use the simplest frontend that elaborates your properties.

| Frontend | Use when | Requirements and limits |
|---|---|---|
| `verilog` | Immediate assertions and simple concurrent assertions | Built into Yosys. Does not correctly support implications, sequence operators or compilation-unit `bind` |
| `slang` | `bind`, `\|->`, `\|=>`, sequences, or richer SystemVerilog | Needs the `yosys-slang` plugin, set by `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN` |

- For concurrent SVA under slang, use a build that lowers the constructs you need. The [rtl-buddy yosys-slang branch](https://github.com/rtl-buddy/yosys-slang/tree/rtl-buddy-sv-elab) supports the property flow: sequences (`##N`, `##[m:n]`, sequence `and`/`or`), `|->`, `|=>` and `disable iff`. Build it against the [rtl-buddy Yosys](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/#install-the-tools) you run. Loaded as a plugin, it replaces the slang frontend built into Yosys 0.71, which rejects several of these constructs.
- With slang, includes and macros inside `synthesis translate_off` regions must still resolve, because slang preprocesses those regions.
- Accepted constructs vary by build, and one rejected construct aborts the whole read. Probe your build one construct at a time before writing a large property set:

```systemverilog
module probe(input logic clk, a, b);
  a1: assert property (@(posedge clk) a |-> b);
  a2: assert property (@(posedge clk) $past(a) |-> b);
  a3: cover property (@(posedge clk) a && b);
endmodule
```
