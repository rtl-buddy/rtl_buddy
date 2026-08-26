## Pass/fail detection

Each `rb axi-profile` subcommand exits with the underlying `axi-profiler` exit code. A non-zero exit means the profiler reported an elaboration, ingest, or write error — check the relevant `.log` file under `artefacts/axi/`.

Missing prerequisites (no `axi_bundles:` in `models.yaml`, no FST at the expected path, no `marimo` for `notebook`) surface as a clear `FatalRtlBuddyError` *before* invoking `axi-profiler`, so the error is anchored at the prerequisite step rather than buried in profiler output.
