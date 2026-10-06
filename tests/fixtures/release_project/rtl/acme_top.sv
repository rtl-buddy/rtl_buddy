// Customer-facing top: its name, ports and parameters are preserved.
module acme_top #(parameter int unsigned SHIFT = 1) (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic        en_i,
  input  logic [7:0]  step_i,
  output logic [15:0] result_o,
  output logic        ovf_o
);
  logic [15:0] core_result;
  acme_core #(.SHIFT(SHIFT)) u_core (
    .clk(clk_i), .rst_n(rst_ni), .en(en_i), .step(step_i), .result(core_result)
  );
  assign result_o = core_result;
  assign ovf_o    = &core_result[15:12];
endmodule
