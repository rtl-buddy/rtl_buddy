// Source of multiblock.fst, the multi-block FST rb saif's tests convert.
// Regenerate: iverilog -o mb.vvp multiblock.v && vvp -n mb.vvp -fst
// $dumpflush every 50 cycles gives several FST blocks; the clock stops before a final
// $dumpall, which repeats every value unchanged at a time nothing toggles.
// multiblock.saif is what the per-variable scan (before the one-pass read) wrote for it.
module tb;
  reg clk = 0;
  reg run = 1;
  always #5 if (run) clk = ~clk;
  reg rst;
  reg [7:0] din;
  wire [7:0] dout;
  wire [3:0] cnt;
  wire bus_z;
  integer i, seed;
  dut u_dut (.clk(clk), .rst(rst), .din(din), .dout(dout), .cnt(cnt), .oe(din[0]), .bus(bus_z));
  initial begin
    $dumpfile("multiblock.fst");
    $dumpvars(0, tb);
    seed = 7;
    rst = 1'bx;
    din = 8'hxx;
    #12 rst = 1;
    #10 rst = 0;
    for (i = 0; i < 300; i = i + 1) begin
      @(negedge clk);
      din = $random(seed);
      if (i % 50 == 49) $dumpflush;
    end
    run = 0;
    #20 $dumpall;
    #3 $finish;
  end
endmodule
module dut(input clk, input rst, input [7:0] din, output reg [7:0] dout, output reg [3:0] cnt, input oe, output bus);
  wire n1 = din[1] & din[2];
  wire n2 = ^din;
  assign bus = oe ? n1 : 1'bz;
  always @(posedge clk) begin
    if (rst) begin dout <= 0; cnt <= 0; end
    else begin dout <= din ^ {7'b0, n2}; cnt <= cnt + 1; end
  end
endmodule
