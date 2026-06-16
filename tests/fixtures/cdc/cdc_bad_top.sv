// cdc_bad_top — NEGATIVE fixture: a genuinely unsynchronized multi-bit
// crossing (clk_a binary counter sampled directly in clk_b, no synchronizer).
// rtl-buddy-cdc flags this as an unprotected bus crossing; an XDC that
// false-paths / clock-groups it is over-waiving a real metastability bug.
module cdc_bad_top (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       rst_n,
    input  logic [7:0] a_data,
    output logic [7:0] b_data
);
  logic [7:0] cnt_a;
  always_ff @(posedge clk_a or negedge rst_n) begin
    if (!rst_n) cnt_a <= '0;
    else        cnt_a <= cnt_a + a_data;
  end
  // Unsynchronized 8-bit crossing: no 2FF, no gray coding.
  always_ff @(posedge clk_b or negedge rst_n) begin
    if (!rst_n) b_data <= '0;
    else        b_data <= cnt_a;
  end
endmodule
