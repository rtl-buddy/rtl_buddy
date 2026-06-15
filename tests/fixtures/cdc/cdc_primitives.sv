// Portable CDC primitives — vendor-neutral.
//
// These carry only synthesis-portable attributes (ASYNC_REG, keep): no XPM
// macros, no UNISIM instantiation. The same RTL elaborates for ASIC and FPGA
// and for both Yosys and Vivado; the CDC exceptions ride in the SDC/XDC, not
// in the RTL. Teaching reference, not production IP.

// Two-flop level synchronizer. Use for single-bit signals, or for a bus
// whose bits are guaranteed to change one-at-a-time (e.g. gray-coded).
module cdc_sync2 #(
    parameter int               WIDTH   = 1,
    parameter logic [WIDTH-1:0] RST_VAL = '0
) (
    input  logic             dst_clk,
    input  logic             dst_rst_n,
    input  logic [WIDTH-1:0] d,    // foreign-domain input
    output logic [WIDTH-1:0] q     // synchronized to dst_clk
);
  (* ASYNC_REG = "TRUE", keep = "true" *) logic [WIDTH-1:0] meta_q;
  (* ASYNC_REG = "TRUE", keep = "true" *) logic [WIDTH-1:0] sync_q;

  always_ff @(posedge dst_clk or negedge dst_rst_n) begin
    if (!dst_rst_n) begin
      meta_q <= RST_VAL;
      sync_q <= RST_VAL;
    end else begin
      meta_q <= d;
      sync_q <= meta_q;
    end
  end

  assign q = sync_q;
endmodule

// Reset synchronizer: asynchronous assert, synchronous deassert into dst_clk.
module cdc_reset_sync (
    input  logic dst_clk,
    input  logic async_rst_n,
    output logic dst_rst_n
);
  (* ASYNC_REG = "TRUE", keep = "true" *) logic ff0, ff1;

  always_ff @(posedge dst_clk or negedge async_rst_n) begin
    if (!async_rst_n) begin
      ff0 <= 1'b0;
      ff1 <= 1'b0;
    end else begin
      ff0 <= 1'b1;
      ff1 <= ff0;
    end
  end

  assign dst_rst_n = ff1;
endmodule

// Four-phase req/ack handshake. The payload bus crosses unsynchronized but is
// held stable by the protocol (sampled in dst only after req has synchronized
// in), so only req and ack need 2-flop synchronizers. The bus crossing is
// covered by a set_max_delay -datapath_only / set_bus_skew exception.
module cdc_handshake #(
    parameter int DATA_W = 8
) (
    input  logic              src_clk,
    input  logic              src_rst_n,
    input  logic              src_valid,
    output logic              src_ready,
    input  logic [DATA_W-1:0] src_data,
    input  logic              dst_clk,
    input  logic              dst_rst_n,
    output logic              dst_valid,   // 1-cycle strobe in dst domain
    output logic [DATA_W-1:0] dst_data
);
  // source domain
  logic              req_q;
  logic [DATA_W-1:0] data_q;     // held stable while req_q is high
  logic              ack_sync;   // ack brought into src domain
  // destination domain
  logic              ack_q;
  logic              req_sync;   // req brought into dst domain
  logic              req_sync_d;

  always_ff @(posedge src_clk or negedge src_rst_n) begin
    if (!src_rst_n) begin
      req_q  <= 1'b0;
      data_q <= '0;
    end else if (!req_q && !ack_sync && src_valid) begin
      req_q  <= 1'b1;
      data_q <= src_data;
    end else if (req_q && ack_sync) begin
      req_q <= 1'b0;
    end
  end

  assign src_ready = !req_q && !ack_sync;

  // ack -> src ; req -> dst
  cdc_sync2 u_ack_sync (
      .dst_clk(src_clk), .dst_rst_n(src_rst_n), .d(ack_q), .q(ack_sync));
  cdc_sync2 u_req_sync (
      .dst_clk(dst_clk), .dst_rst_n(dst_rst_n), .d(req_q), .q(req_sync));

  always_ff @(posedge dst_clk or negedge dst_rst_n) begin
    if (!dst_rst_n) begin
      ack_q      <= 1'b0;
      req_sync_d <= 1'b0;
      dst_valid  <= 1'b0;
      dst_data   <= '0;
    end else begin
      req_sync_d <= req_sync;
      dst_valid  <= 1'b0;
      if (req_sync && !req_sync_d) begin
        dst_data  <= data_q;   // quasi-static bus crossing (held by protocol)
        dst_valid <= 1'b1;
        ack_q     <= 1'b1;
      end else if (!req_sync) begin
        ack_q <= 1'b0;
      end
    end
  end
endmodule
