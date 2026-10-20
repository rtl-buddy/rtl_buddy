## Extract parasitics with rcx-rules

`rcx-rules` is a path, resolved like `pdn-config`, to an OpenRCX extraction-rules file (ORFS `RCX_RULES`). Its `LayerCount` must match the technology LEF's routing stack.

With it set, the flow extracts parasitics after fill insertion, writes `<top>.routed.spef`, and times the final reports on that SPEF instead of the global-route estimate. The summary's WNS and TNS come from the extracted parasitics, and a [`netlist-source: pnr` power run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/#extracted-parasitics) reads the same SPEF. With the key unset there is no extraction.
