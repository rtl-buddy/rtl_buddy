## Constrain reset and keep covers reachable

For a stateful design without initialized registers, constrain reset at the initial cycle in `constraints`:

```systemverilog
module fpv_reset_pin(input logic clk, rst_n);
  logic f_init = 1'b1;
  always_ff @(posedge clk) f_init <= 1'b0;
  assume property (@(posedge clk) f_init |-> !rst_n);
endmodule

bind dut fpv_reset_pin u_reset_pin(.clk(clk), .rst_n(rst_n));
```

This uses `bind`, so it needs `frontend: slang`. If the constraint makes a cover's target states unreachable, put covers in a separate verification without it:

```yaml
- name: fifo_assertions
  constraints: reset_pin.sv
  properties: [fifo_asserts.sv]
- name: fifo_covers
  properties: [fifo_covers.sv]
```

Match `disable iff` polarity to the design reset; for an active-low reset use `disable iff (!rst_n)`.
