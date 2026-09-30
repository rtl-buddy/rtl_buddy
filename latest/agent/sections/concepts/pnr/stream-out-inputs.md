## Stream-out inputs

- **Layout.** Cell layouts come from the PDK's `cell-gds` (one path or a list), then the run's `gds-paths`. Put a hard macro's layout in `gds-paths`; an OpenRAM SRAM has its LEF in `lef-paths` and its GDS in `gds-paths`. Paths resolve against the file that names them.
- **LEFs.** The DEF reader gets the technology LEF, the PDK's macro LEF, then the run's `lef-paths`.
- **Record.** `def2stream.inputs.json` in the artefact directory shows what a run streamed.

A configured input that is missing on disk stops the export before KLayout starts, with every missing path reported at once.
