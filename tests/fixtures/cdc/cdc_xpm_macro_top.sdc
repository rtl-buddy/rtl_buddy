# XPM CDC macro fixture: two asynchronous clock domains, every crossing
# handled by an xpm_cdc_* macro.
create_clock -name clk_a -period 8.0  [get_ports clk_a]
create_clock -name clk_b -period 10.0 [get_ports clk_b]
set_input_delay -clock clk_a 1.0 [get_ports {a_flag a_incr}]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
