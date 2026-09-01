## Interpret results

Mapped runs report gates and area. Constrained Yosys runs report WNS as clock period minus critical-path delay. OpenROAD reports actual WNS and TNS; negative values indicate violations and TNS 0 indicates no negative endpoint slack.

A Yosys run passes when the process exits 0, its log has no `ERROR:` line, and neither correctness gate fires. An OpenROAD run requires both the Yosys and OpenROAD stages to exit 0 and rejects OpenROAD `[ERROR ...]` lines. Any failed stage reports `FAIL`.
