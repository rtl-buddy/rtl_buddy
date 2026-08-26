## FPV COI analysis is best-effort

A cone-of-influence Yosys failure logs `fpv coi_yosys_failed`, omits COI data, and does not fail a successful proof. If COI numbers disappear, inspect `artefacts/<name>/coi.log` and verify `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN`.
