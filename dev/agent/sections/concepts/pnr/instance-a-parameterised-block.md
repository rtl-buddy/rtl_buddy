## Instance a parameterised block

A hardened block has one parameter set, its synthesis's `params:`. A parent that writes `blk #(.W(16)) u_blk (...)` needs a stub that declares the parameters:

```systemverilog
(* blackbox *)
module blk #(parameter int W = 8) (input logic clk, input logic [W-1:0] d, output logic [W-1:0] q);
endmodule
```

Use `frontend: slang` for such a parent. Before OpenROAD reads it, rb checks each instance against the block and rewrites it to `blk u_blk (...)`:

- every connected port must exist in the abstract with the same width;
- every input must be connected;
- each override must equal the value in `<top>.params.json`.

A mismatch stops the run naming the instance and both values. Re-harden a block that has no record.
