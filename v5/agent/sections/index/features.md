## Features

- Run individual Verilog/SystemVerilog tests or full regressions from YAML config files
- Randomized seed testing with repeat and replay support
- Plugin hooks for sweep generation, test pre-processing, and post-processing
- Filelist generation from `models.yaml`
- Yosys synthesis (`rb synth`) with optional Liberty mapping, a yosys-slang frontend, and an OpenROAD backend
- OpenROAD place-and-route (`rb pnr`) and gate-level power analysis (`rb power`, with `rb saif` activity capture)
- CDC lint (`rb cdc`) via [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc)
- Formal property verification (`rb fpv`) with SymbiYosys, solver pinning, and `rb wave-fpv` counterexample viewing
- Mutation testing (`rb mut`) that scores a verification suite by mutating a design and checking whether an FPV or simulation/assertion oracle kills each mutant
- Waveform viewing (`rb wave`) in [Surfer](https://surfer-project.org/) with live editor annotation, and module hierarchy rendering (`rb hier`)
- AXI interconnect profiling (`rb axi-profile`) with a packaged marimo notebook, and a coordination hub (`rb hub`) tying the view SPA, Surfer, and editors together
- Spec traceability (`rb spec`) and a declarative external-tool readiness check (`rb tool-check`)
- Verilator coverage collection, merge, summary, and export workflows
- Verible command integration (`rb verible`) for lint, syntax, formatting, preprocessing, and `verible.filelist` generation
- Machine-readable JSONL logging for use with AI agents and CI pipelines

`rtl_buddy` can be adapted to different project toolchains, but the primary supported simulation flows are Verilator and VCS, and synthesis through Yosys. Broader first-class Verible and PeakRDL workflows are on the roadmap.
