## Install the formal toolchain

Install `sby` and at least one solver, then run `rb tool-check --required-for fpv`. The [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) bundles Yosys, SymbiYosys and solvers. [rtl-buddy-tools](https://github.com/rtl-buddy/rtl-buddy-tools) builds SymbiYosys, Yices, Boolector and Z3 at pinned versions for macOS and Linux. Otherwise follow the upstream SymbiYosys instructions and put a solver such as Yices, Z3, Boolector or ABC on `PATH`.

Only the `sby` backend is supported. If it is not on `PATH`, set its absolute path in `cfg-fpv-tools`.
