// 4-bit counter — minimal SV-2012 design used as the Icarus smoke target.
// Deliberately avoids classes, SVA, and covergroups so it compiles on
// both Verilator and Icarus 12.
module counter #(
  parameter int WIDTH = 4
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             enable,
  output logic [WIDTH-1:0] count
);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)       count <= '0;
    else if (enable)  count <= count + 1'b1;
  end

endmodule
