## Instance a parameterised block

A hardened block has one parameter set, its synthesis's `params:`. A parent that writes `blk #(.W(16)) u_blk (...)` needs a stub that declares the parameters:

```systemverilog
(* blackbox *)
module blk #(parameter int W = 8) (input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);
endmodule
```

Use `frontend: slang` for such a parent. rb rewrites each instance to `blk u_blk (...)` before OpenROAD reads it, after checking it against the block: every connected port must exist in the abstract with the same width, every input must be connected, and each override must equal the value recorded in `<top>.params.json`. A mismatch stops the run naming the instance and both values. Re-harden a block with no record.
