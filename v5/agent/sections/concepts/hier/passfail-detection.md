## Pass/fail detection

`rb hier` exits with the renderer's exit code. A non-zero exit means the renderer reported a parse, elaboration, or output error — check `hier.log` for the captured stderr.

The `Failed to locate rtl-buddy-view` error before the renderer runs is the most common failure mode and indicates that `rtl-buddy-view` is not installed in the active venv or on `PATH`. Run `rb tool-check --explain rtl-buddy-view` for the install hint.
