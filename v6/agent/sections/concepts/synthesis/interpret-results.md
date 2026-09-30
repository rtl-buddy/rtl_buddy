## Interpret results

Mapped runs report gates and area. A constrained Yosys run reports WNS as clock period minus critical-path delay. OpenROAD reports actual WNS and TNS: negative values are violations, and TNS 0 means no endpoint has negative slack.

A Yosys run passes when the process exits 0, the log has no `ERROR:` line, and no correctness gate fires. An OpenROAD run also needs the OpenROAD stage to exit 0 with no `[ERROR ...]` line. Any failed stage reports `FAIL`; read that stage's log first.
