## Artifact Layout

Generated outputs should live under `artefacts/<name>/` below the command root.
Repeated or randomized runs should use stable run directories such as `run-0001`, `run-0002`, and so on.
Convenience symlinks may point at the latest run, but they must not be the only durable location.

Compile-side generated files such as `run.f`, `compile.log`, builder outputs, and relative `builder-simv` paths belong in the per-test artifact root.
Simulation outputs for `randtest` belong in the per-run artifact directory to avoid side-file clobbering across iterations.
