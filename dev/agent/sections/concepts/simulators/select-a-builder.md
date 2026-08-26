## Select a builder

Builder precedence is `--builder <name>`, per-test `builder:`, suite-level `builder:`, then the platform default.

The selected `cfg-rtl-builder` entry should set `simulator-family`, or use an executable name from which RTL Buddy can infer it. See [Selecting the simulator builder](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#selecting-the-simulator-builder).
