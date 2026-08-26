## AXI profiling converts VCS VPD traces

`rb axi-profile run` converts `vcdplus.vpd` with `vpd2vcd`, trying `-full64` first and the legacy form second. Conversion details go to `artefacts/axi/<test>/vpd-convert.log`.

If `vcd2fst` is installed, the VCD becomes a cached `vcdplus.fst`; otherwise rtl_buddy keeps and ingests the larger VCD with a warning. Cached files live beside the original VPD in the test artifact directory.
