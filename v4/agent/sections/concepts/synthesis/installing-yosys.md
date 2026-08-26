## Installing Yosys

`rtl_buddy` uses the [rtl-buddy fork of Yosys](https://github.com/rtl-buddy/yosys), which tracks upstream with rtl-buddy-specific patches. Build from source:

```bash
# Install deps with brew. Adjust to your package manager as needed.
brew install cmake python tcl-tk libffi readline

# Clone, build and install
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang   # or `make config-gcc` on Linux
make -j 8           # adjust to no. of CPU cores if needed
make install        # installs to /usr/local/bin/yosys
```

Verify the install:

```bash
yosys --version
```

The `yosys` binary must be on `PATH` when `rb synth` is invoked.

### Optional: yosys-slang plugin

For designs that use SystemVerilog-2017 features Yosys's built-in frontend doesn't accept (e.g. `import pkg::*`, packed-struct typedefs, complex package generates), build the [yosys-slang](https://github.com/povik/yosys-slang) plugin against the same Yosys you just installed:

```bash
git clone --recursive https://github.com/povik/yosys-slang.git
cd yosys-slang
make -j 8           # produces build/slang.so
make install        # optional: copies into $(yosys-config --datdir)/plugins/
```

Wire it into `rb synth` by setting `opts.frontend: "slang"` and `opts.plugin-path` under `cfg-synth-tools` (see [`SystemVerilog frontend`](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/synthesis/#systemverilog-frontend) below). Skip this step entirely if your designs work with the default `frontend: "verilog"`.
