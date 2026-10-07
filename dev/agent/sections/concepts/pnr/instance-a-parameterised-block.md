## Instance a parameterised block

A parent instances a parameterised block with an override such as `blk #(.W(16)) u_blk (...)`. Its stub declares the block's parameters, so the frontend accepts the override and sizes the ports:

```systemverilog
// blk_bb.sv, on the parent's filelist. The block was synthesised with params: {W: 16}.
(* blackbox *)
module blk #(parameter int W = 8, localparam int AW = $clog2(W)) (
  input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);
endmodule
```

Yosys then writes the instance in a form OpenROAD cannot use:

- With `frontend: slang` it writes `blk #(.AW(32'd4), .W(32'd16)) u_blk (...)`. That is every parameter, localparams included, and OpenROAD rejects the syntax (`STA-0171`).
- With the native `verilog` frontend it derives a copy of the blackbox, `\$paramod\blk\W=s32'0...10000 u_blk (...)`, and OpenROAD has no master of that name. With many overrides Yosys names the copy by a hash, `\$paramod$<sha1>\blk`. The values cannot be read back from that name, so such an instance fails the run with a request to use `frontend: slang`.

rtl_buddy rewrites each instance as `blk u_blk (...)` before OpenROAD reads the netlist:

- `rb synth` with `blocks:` rewrites `synth_netlist.v`. Yosys does not read the block's abstract Liberty when the filelist has a parameterised `(* blackbox *)` stub of the module, because the Liberty would replace the stub and lose its parameters.
- `rb pnr` rewrites any instances that remain, for example in a netlist from a synthesis without `blocks:`, and reads the result as `pnr_netlist.v` in its artefact directory.
- `rb power` takes its blocks from the synthesis it reads, so it checks and rewrites its own `power_netlist.v` copy only when that synthesis has `blocks:`.

Each of these steps checks every instance of a block before rewriting it, so the rewrite does not hide a parent that disagrees with the hardened block. A failed check stops the run before OpenROAD reads the netlist, and its message names the instance, its line, and both values.

- **Ports match the abstract.** Every port the instance connects must be a pin or bus of the block's abstract LEF, with the same width (`port 'd' is connected to 16 bit(s), but the hardened block's pin is 8 bit(s) wide`). Every signal pin of the abstract must be connected.
  - An unconnected input or inout fails; an unconnected output is a warning. Supply pins are not checked here.
  - Widths come from the netlist's declarations, selects, concatenations and sized constants.
  - A connection rb cannot size, a positional port list or an instance array fails with the connection named.
  - This is the strongest check. Any parameter that changes a port's width is caught here, recorded or not.
- **Overrides match the elaborated block, as far as the record goes.** Every `harden: true` run elaborates the block's top once more in Yosys (scratch files in `param_probe/` of the run's artefact directory) and records its parameter values as `<top>.params.json` in the abstract. Each override on an instance, named or positional, is compared with its recorded value as a whole number; two literals of the same width compare by their bits. A real, recorded as Yosys's string (`"2.500000"`), compares numerically with the netlist's `2.500000`. All instances of a block must also agree. What the record covers depends on the block's frontend:
  - **`frontend: slang`**: the record lists the top's parameters and localparams, so an override the block does not have fails. A real-valued parameter cannot be recorded; the abstract is then published without a record.
  - **native `verilog` frontend**: the record lists parameters but not localparams, so an override the record lacks is only a warning. A `real` parameter is recorded only when the synthesis's `params:` sets it.
  - **No record**: an abstract hardened by an older rtl_buddy, or one whose top Yosys could not elaborate (warning `pnr.block_params_unrecorded`). Only the synthesis's `params:` are compared, with a warning to re-harden. A YAML integer, or a quoted unsized number such as `"-1"`, matches an override of width W when it fits W bits, signed or unsigned, and agrees modulo 2^W.
  - Warnings are reported once per block and parameter, with a count of the instances.
- **The rewrite removes nothing else.** Outside the rewritten statements the output is byte-identical to the input. Each rewritten statement, read back from the output, has the input's tokens minus the `#(...)` list and with the master renamed, and needs no further rewrite. Anything else is an internal error, not a silent edit.

The record is an abstract view, so editing or losing it makes the abstract stale. Re-harden a block (`rb pnr <block run>`) to give it a record.
