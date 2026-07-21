// cdc_xpm_top — XPM-style recognition fixture. `xpm_cdc_single` here is a
// stand-in (a single flop, deliberately under-modelled) for a vendor macro
// the analyzer does NOT recognize structurally — so it flags the crossing as
// a violation. The real macro IS a synchronizer; --check-xdc's recognized-syncs
// list is how the user tells the audit so (pairs with blackboxing the macro).
module xpm_cdc_single (
    input  logic dest_clk,
    input  logic src_in,
    output logic dest_out
);
  logic ff;  // one flop only — the analyzer sees an insufficient synchronizer
  always_ff @(posedge dest_clk) ff <= src_in;
  assign dest_out = ff;
endmodule

module cdc_xpm_top (
    input  logic clk_a,
    input  logic clk_b,
    input  logic a_flag,
    output logic b_flag
);
  logic a_flag_q;
  always_ff @(posedge clk_a) a_flag_q <= a_flag;
  xpm_cdc_single u_xpm_single (
      .dest_clk(clk_b), .src_in(a_flag_q), .dest_out(b_flag)
  );
endmodule
