## Features

- Run individual Verilog/SystemVerilog tests or full regressions from YAML config files
- Randomized seed testing with repeat and replay support
- Plugin hooks for sweep generation, test pre-processing, and post-processing
- Filelist generation from `models.yaml`
- Yosys synthesis runs and synthesis regressions from `synth.yaml`
- Verilator coverage collection, merge, summary, and export workflows
- Basic Verible command integration for lint, syntax, formatting, and preprocessing
- Machine-readable JSONL logging for use with AI agents and CI pipelines

`rtl_buddy` can be adapted to different project toolchains, but the primary supported simulation flows are Verilator and VCS, and synthesis through Yosys. Broader first-class Verible and PeakRDL workflows are on the roadmap.
