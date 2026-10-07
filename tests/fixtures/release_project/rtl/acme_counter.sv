`include "acme_defs.svh"
// Secret accumulator: adds a scaled step every enabled cycle.
module acme_counter import acme_pkg::*; (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       en,
  input  acme_word_t step,
  output logic [`ACME_ACC_W-1:0] acc_q
);
  logic [`ACME_ACC_W-1:0] acc_nxt;
  // A field named like a built-in method: Verible leaves `.min` unrenamed.
  struct packed { logic [`ACME_ACC_W-1:0] min; } lim;
  assign lim.min = acc_q;
  // Language-predefined macros keep their names.
  initial if (0) $display("%s:%0d", `__FILE__, `__LINE__);
`ifndef SYNTHESIS
  // Built-in methods keep their names.
  int unsigned seen_q[$];
  always_ff @(posedge clk) if (en) begin
    seen_q.push_back(acc_q);
    if (seen_q.size() > 4) void'(seen_q.pop_front());
  end
`endif
  always_comb acc_nxt = acc_q + scaled_step(step);
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) acc_q <= '0;
    else if (en) acc_q <= acc_nxt;
endmodule
