## Features

- Run individual tests or full regressions from YAML config files
- Randomized seed testing with repeat support
- Plugin hooks for sweep generation, test pre-processing, and post-processing
- Filelist generation from `models.yaml`
- Basic Verible command integration for lint, syntax, formatting, and preprocessing
- Machine-readable JSONL logging for use with AI agents and CI pipelines

`rtl_buddy` can be adapted to different project toolchains, but the primary supported flows are Verilator and VCS. Broader first-class Verible and PeakRDL workflows are on the roadmap.
