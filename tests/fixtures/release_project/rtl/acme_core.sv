// Core: two private accumulators combined.
module acme_core #(parameter int unsigned SHIFT = 1) (
  input  logic        clk,
  input  logic        rst_n,
  input  logic        en,
  input  logic [7:0]  step,
  output logic [15:0] result
);
  logic [11:0] acc_a, acc_b;
  acme_counter u_cnt_a (.clk, .rst_n, .en, .step(step),           .acc_q(acc_a));
  acme_counter u_cnt_b (.clk, .rst_n, .en, .step(step + 8'd1),    .acc_q(acc_b));
`ifdef ACME_FAST
  assign result = {4'd0, acc_a} + ({4'd0, acc_b} << SHIFT);
`else
  assign result = {4'd0, acc_a};
`endif
endmodule
