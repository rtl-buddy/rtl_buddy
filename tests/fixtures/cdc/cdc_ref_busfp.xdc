create_clock -name clk_a -period 8.0  [get_ports {clk_a}]
create_clock -name clk_b -period 10.0 [get_ports {clk_b}]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
