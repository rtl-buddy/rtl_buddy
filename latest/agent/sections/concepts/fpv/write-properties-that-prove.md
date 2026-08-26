## Write properties that prove

`bmc` checks behavior only to `depth`. `prove` uses temporal k-induction and may start its induction step from unreachable states that satisfy the assumptions and prior assertion hypotheses. An `UNKNOWN` trace may therefore be a real reachable bug or a counterexample to induction.

When `prove` returns `UNKNOWN`:

1. Open the induction trace and determine whether its initial state is reachable.
2. Add invariants that exclude impossible predecessor states or express required relationships between pipeline stages.
3. Check environment assumptions; an under-constrained environment creates false failures, while an over-constrained one hides bugs.
4. Increase depth only when the design legitimately needs more steps to become inductive. A depth-dependent proof can be fragile across design changes.

All assertions strengthen the induction hypothesis together. A companion invariant can close another property, but keep each assertion meaningful and validate it independently where practical.

For stateful designs without initialized registers, constrain reset at the initial cycle. Put the reusable environment assumption in `constraints`:

```systemverilog
module fpv_reset_pin(input logic clk, rst_n);
  logic f_init = 1'b1;
  always_ff @(posedge clk) f_init <= 1'b0;
  assume property (@(posedge clk) f_init |-> !rst_n);
endmodule

bind dut fpv_reset_pin u_reset_pin(.clk(clk), .rst_n(rst_n));
```

This example requires `frontend: slang` because it uses `bind`. Keep unconstrained reachability covers in a separate verification if the reset pin makes their target states unreachable:

```yaml
- name: fifo_assertions
  constraints: reset_pin.sv
  properties: [fifo_asserts.sv]
- name: fifo_covers
  properties: [fifo_covers.sv]
```

Match `disable iff` polarity to the design reset. For an active-low reset, use `disable iff (!rst_n)`.
