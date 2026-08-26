## FPGA bitstream generation relaxes two I/O DRCs

Before `write_bitstream`, rtl_buddy downgrades Vivado NSTD-1 and UCIO-1 so bring-up designs without a complete pinout can produce a bitstream. The earlier DRC report and machine result retain their original severity. Treat either violation as blocking for real hardware and add the missing `IOSTANDARD` and `LOC` constraints.
