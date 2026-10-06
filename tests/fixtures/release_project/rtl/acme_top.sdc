# Block constraints, written against the preserved top ports.
create_clock -name clk_i -period 5.0 [get_ports clk_i]
foreach p {en_i step_i*} {
  set_input_delay 1.0 -clock clk_i [get_ports $p]
}
set_output_delay 1.0 -clock clk_i [get_ports {result_o* ovf_o}]
set_false_path -from [get_ports rst_ni]
