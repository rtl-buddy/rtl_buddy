## Configure the builder

Each `cfg-rtl-builder` entry owns:

- the simulator executable and compiled `simv` path;
- simulator family and seed syntax;
- named compile-time and run-time option sets;
- optional builder-specific timeout allowances and waveform format.

Tests select a builder through the CLI, test or suite config, then platform default. See [Simulation Backends](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/simulators/#select-a-builder) and the [root config schema](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml).

Keep Surfer editor and socket settings under `cfg-surfer`; [Waveform Viewer](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/wave/#configure-surfer-and-the-editor) owns that workflow.
