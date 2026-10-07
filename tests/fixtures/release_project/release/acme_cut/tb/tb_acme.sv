// Customer release testbench: drives the preserved top ports only.
module tb_acme;
  logic clk_i = 0, rst_ni = 0, en_i = 0;
  logic [7:0] step_i = 8'd5;
  logic [15:0] result_o;
  logic ovf_o;
  acme_top dut (.*);
  always #5 clk_i = ~clk_i;
  initial begin
    repeat (2) @(posedge clk_i);
    rst_ni = 1;
    en_i = 1;
    repeat (10) @(posedge clk_i);
    #1;
    $display("SIG: result_o=%0d ovf_o=%0d", result_o, ovf_o);
    if (result_o == 16'd561) $display("TEST PASSED");
    else $display("TEST FAILED: result_o=%0d", result_o);
    $finish;
  end
endmodule
