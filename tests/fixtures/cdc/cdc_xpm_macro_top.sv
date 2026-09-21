// cdc_xpm_macro_top — a design that synchronises with the real Xilinx XPM CDC
// macros, as a Vivado project actually would (rtl_buddy#315).
//
// Contrast with `cdc_xpm_top`, the older fixture in this directory: that one
// models `xpm_cdc_single` as a bare single flop with only a `dest_clk`,
// deliberately under-modelled so the analyzer flags it — it exists to
// demonstrate the `--check-xdc --recognize-sync` escape hatch, which is how a
// user told the AUDIT about a macro the ENGINE could not recognise.
//
// This fixture is the post-fix shape. The stubs are port- and
// parameter-faithful to UG974 and marked `(* blackbox *)`, which is what the
// analyzer really sees: XPM sources ship inside the vendor install tree and
// are injected at synthesis, so a filelist built from project RTL carries only
// the instantiation. rtl-buddy-cdc >= 0.4.0 recognises the `xpm_cdc_*` family
// by module name and classifies these crossings as synchronised; older
// releases decline each macro as a dual-clock blackbox and report CDC-BBX.
//
// Crossings demonstrated:
//   1. single-bit control flag  clk_a -> clk_b  -> xpm_cdc_single
//   2. multi-bit gray counter   clk_a -> clk_b  -> xpm_cdc_gray

(* blackbox *)
module xpm_cdc_single #(
    parameter integer DEST_SYNC_FF   = 4,
    parameter integer INIT_SYNC_FF   = 0,
    parameter integer SIM_ASSERT_CHK = 0,
    parameter integer SRC_INPUT_REG  = 1
) (
    input  wire src_clk,
    input  wire src_in,
    input  wire dest_clk,
    output wire dest_out
);
endmodule

(* blackbox *)
module xpm_cdc_gray #(
    parameter integer DEST_SYNC_FF          = 4,
    parameter integer INIT_SYNC_FF          = 0,
    parameter integer REG_OUTPUT            = 0,
    parameter integer SIM_ASSERT_CHK        = 0,
    parameter integer SIM_LOSSLESS_GRAY_CHK = 0,
    parameter integer WIDTH                 = 2
) (
    input  wire             src_clk,
    input  wire [WIDTH-1:0] src_in_bin,
    input  wire             dest_clk,
    output wire [WIDTH-1:0] dest_out_bin
);
endmodule

module cdc_xpm_macro_top #(
    parameter int CNT_W = 8
) (
    input  logic              clk_a,
    input  logic              clk_b,
    // source-domain (clk_a) stimulus
    input  logic              a_flag,
    input  logic              a_incr,
    // destination-domain (clk_b) observation
    output logic              b_flag,
    output logic [CNT_W-1:0]  b_count
);

    // --- clk_a source registers -------------------------------------------
    logic              a_flag_q;
    logic [CNT_W-1:0]  a_count;

    always_ff @(posedge clk_a) a_flag_q <= a_flag;
    always_ff @(posedge clk_a) a_count  <= a_count + {{(CNT_W-1){1'b0}}, a_incr};

    // --- crossing 1: single-bit control flag ------------------------------
    logic b_flag_sync;
    xpm_cdc_single #(
        .DEST_SYNC_FF (4),
        .SRC_INPUT_REG(0)
    ) u_flag_sync (
        .src_clk (clk_a),
        .src_in  (a_flag_q),
        .dest_clk(clk_b),
        .dest_out(b_flag_sync)
    );

    // --- crossing 2: multi-bit counter, gray-coded by the macro -----------
    logic [CNT_W-1:0] b_count_sync;
    xpm_cdc_gray #(
        .DEST_SYNC_FF(4),
        .WIDTH       (CNT_W)
    ) u_count_sync (
        .src_clk     (clk_a),
        .src_in_bin  (a_count),
        .dest_clk    (clk_b),
        .dest_out_bin(b_count_sync)
    );

    // --- clk_b consumers ---------------------------------------------------
    always_ff @(posedge clk_b) b_flag  <= b_flag_sync;
    always_ff @(posedge clk_b) b_count <= b_count_sync;

endmodule
