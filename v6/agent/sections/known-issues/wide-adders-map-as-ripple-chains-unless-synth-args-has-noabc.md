## Wide adders map as ripple chains unless `synth-args` has `-noabc`

Yosys `synth` runs its generic `abc` pass before the mapped-run ABC step, and that pass's script includes `dc2`, which rebuilds log-depth adders, negates and incrementers as ripple chains. The mapped-run default script omits `dc2`, but it cannot restore depth the earlier pass removed. Add `-noabc` to the effort's `synth-args` for timing-critical datapaths. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#choose-the-mapped-run-abc-script).
