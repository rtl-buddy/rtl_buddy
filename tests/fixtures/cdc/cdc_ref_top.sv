// cdc_ref_top — portable CDC IP exercised across two asynchronous
// clocks (clk_a -> clk_b). Instantiates each canonical crossing type so the
// CDC tools have something to recognize and the FPGA flow has something to
// constrain. See README.md.
//
// Crossings demonstrated:
//   1. single-bit level   (control flag)            -> cdc_sync2 #(1)
//   2. multi-bit gray bus  (counter)                 -> cdc_sync2 #(W) on gray
//   3. req/ack handshake   (data word)              -> cdc_handshake
//   4. reset synchronizer  (clk_b reset)            -> cdc_reset_sync

module cdc_ref_top #(
    parameter int CNT_W  = 8,
    parameter int DATA_W = 8
) (
    input  logic              clk_a,
    input  logic              clk_b,
    input  logic              arst_n,       // async reset, both domains
    // source-domain (clk_a) stimulus
    input  logic              a_flag,
    input  logic              a_send,
    input  logic [DATA_W-1:0] a_data,
    // destination-domain (clk_b) outputs
    output logic              b_flag,
    output logic [CNT_W-1:0]  b_count,
    output logic              b_valid,
    output logic [DATA_W-1:0] b_data,
    output logic              a_ready
);
  // --- reset synchronizers (one per domain) ---
  logic rst_n_a, rst_n_b;
  cdc_reset_sync u_rst_a (.dst_clk(clk_a), .async_rst_n(arst_n), .dst_rst_n(rst_n_a));
  cdc_reset_sync u_rst_b (.dst_clk(clk_b), .async_rst_n(arst_n), .dst_rst_n(rst_n_b));

  // --- 1. single-bit level crossing ---
  // Register the primary input in its source domain first: a 2FF synchronizer
  // must sample a registered source, never combinational logic or a raw port
  // (a glitch on the D pin can be captured as a real edge).
  logic a_flag_q;
  always_ff @(posedge clk_a or negedge rst_n_a) begin
    if (!rst_n_a) a_flag_q <= 1'b0;
    else          a_flag_q <= a_flag;
  end
  cdc_sync2 #(.WIDTH(1)) u_flag_sync (
      .dst_clk(clk_b), .dst_rst_n(rst_n_b), .d(a_flag_q), .q(b_flag));

  // --- 2. multi-bit gray-coded counter crossing ---
  logic [CNT_W-1:0] bin_a, gray_a, gray_b, bin_b;

  always_ff @(posedge clk_a or negedge rst_n_a) begin
    if (!rst_n_a) bin_a <= '0;
    else          bin_a <= bin_a + 1'b1;
  end
  assign gray_a = bin_a ^ (bin_a >> 1);  // bin -> gray (1 bit changes/step)

  cdc_sync2 #(.WIDTH(CNT_W)) u_gray_sync (
      .dst_clk(clk_b), .dst_rst_n(rst_n_b), .d(gray_a), .q(gray_b));

  // gray -> bin in the destination domain
  always_comb begin
    bin_b[CNT_W-1] = gray_b[CNT_W-1];
    for (int i = CNT_W - 2; i >= 0; i--) bin_b[i] = bin_b[i+1] ^ gray_b[i];
  end
  assign b_count = bin_b;

  // --- 3. req/ack handshake passing a data word ---
  cdc_handshake #(.DATA_W(DATA_W)) u_hs (
      .src_clk  (clk_a),
      .src_rst_n(rst_n_a),
      .src_valid(a_send),
      .src_ready(a_ready),
      .src_data (a_data),
      .dst_clk  (clk_b),
      .dst_rst_n(rst_n_b),
      .dst_valid(b_valid),
      .dst_data (b_data)
  );
endmodule
