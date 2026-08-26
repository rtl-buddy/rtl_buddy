## Interpret results

The summary reports cell count, design area, setup and hold WNS, and the number of non-empty DRC report lines. Positive slack meets timing; zero DRC lines indicate a clean route.

A run passes when OpenROAD exits 0 and emits no `[ERROR ...]` line. It skips when filtered by `reglvl` or when `tool:` is unsupported. Timing violations or DRC counts are reported as metrics; inspect the result policy for your project before using them as signoff gates.
