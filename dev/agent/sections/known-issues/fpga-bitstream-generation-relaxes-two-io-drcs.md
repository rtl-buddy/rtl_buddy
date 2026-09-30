## FPGA bitstream generation relaxes two I/O DRCs

Before `write_bitstream`, rtl_buddy downgrades Vivado DRCs NSTD-1 and UCIO-1 so designs without a complete pinout can produce a bitstream. For real hardware, treat either violation as blocking and add the missing `IOSTANDARD` and `LOC` constraints.
