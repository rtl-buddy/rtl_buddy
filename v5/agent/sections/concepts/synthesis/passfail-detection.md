## Pass/fail detection

**Yosys backend:** a run passes when the tool exits with code 0 and `synth.log` contains no lines starting with `ERROR:`.

**OpenROAD backend:** both stages must succeed. The Yosys stage applies the same exit-code and `ERROR:` check; the OpenROAD stage checks exit code and the absence of `[ERROR ...]` lines in `synth.log`.

Any other outcome is **FAIL** with a description in the results table.
