## Install the formal toolchain

Install `sby` and at least one solver, then run `rb tool-check --required-for fpv`. The [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) bundles Yosys, SymbiYosys, and solvers. Otherwise follow the upstream SymbiYosys installation instructions and put a solver such as Yices, Z3, Boolector, or ABC on `PATH`.

Only the `sby` backend is supported. Configure its executable as an absolute path in `cfg-fpv-tools` when it is not on `PATH`.
