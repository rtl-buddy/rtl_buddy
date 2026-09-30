## Configure the builder

Each `cfg-rtl-builder` entry sets:

- the simulator executable and the compiled `simv` path;
- the simulator family and seed syntax;
- named compile-time and run-time option sets;
- optionally, timeout allowances and the waveform format.

A test's builder comes from the CLI, then the test or suite config, then the platform default. See [Simulation Backends](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/simulators/#select-a-builder) and the [root config schema](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml). Surfer editor and socket settings live under `cfg-surfer`; see [Waveform Viewer](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/wave/#configure-surfer-and-the-editor).
