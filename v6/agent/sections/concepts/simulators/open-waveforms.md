## Open waveforms

Verilator writes `dump.fst` and Icarus writes `dump.vcd`. `rb wave` opens the newest supported dump under `artefacts/<test>/`.

To convert an Icarus dump to FST after simulation, set `wave-format: fst-postproc` on the builder. If `vcd2fst` is missing, the VCD is kept.
