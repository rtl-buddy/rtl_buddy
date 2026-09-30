## Extracted parasitics

A `pnr` source reads the P&R run's extracted `<top>.routed.spef` when the PDK sets [`rcx-rules`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#tune-the-process-dependent-steps). Otherwise it estimates parasitics from global routing.

The SPEF is used only if its `pnr.tcl` contains `write_spef` and the SPEF is no older than that `pnr.tcl`. Otherwise the run logs a `power.spef_rejected` warning naming the reason and uses the estimate. The `parasitics` result field (`spef` or `estimated`) and the summary's Parasitics column show which was used, and one ODB measured both ways counts as two experiments.

Extraction usually raises switching power and lowers slack compared with the estimate.
