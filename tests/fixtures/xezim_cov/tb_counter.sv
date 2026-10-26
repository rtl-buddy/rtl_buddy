// xezim coverage fixture: the icarus_smoke counter with one labelled and one
// unlabelled cover property. Regenerate coverage.json (xezim 0.11.3) from this dir:
//   XEZIM_COV_DB=coverage.json xezim --code-coverage=all -s tb_counter counter.sv tb_counter.sv
module tb_counter;
  logic       clk = 0;
  logic       rst_n = 0;
  logic       enable = 0;
  logic [3:0] count;

  counter #(.WIDTH(4)) dut (.clk(clk), .rst_n(rst_n), .enable(enable), .count(count));

  always #5 clk = ~clk;

  c_wrap: cover property (@(posedge clk) count == 4'hF);
  cover property (@(posedge clk) count == 4'h3);

  initial begin
    #23 rst_n = 1;
    enable = 1;
    repeat (10) @(posedge clk);
    enable = 0;
    repeat (2) @(posedge clk);
    $finish;
  end
endmodule
