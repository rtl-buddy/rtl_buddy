## Open waveforms

Verilator normally writes `dump.fst`; Icarus writes `dump.vcd`. `rb wave` opens the newest supported dump under `artefacts/<test>/`.

Set `wave-format: fst-postproc` on an Icarus builder to run `vcd2fst` after simulation. If `vcd2fst` is unavailable, the VCD remains usable.
