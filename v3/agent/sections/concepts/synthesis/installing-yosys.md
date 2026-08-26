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
