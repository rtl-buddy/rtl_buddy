## Install the tools

rtl_buddy is validated against the [RTL Buddy Yosys fork](https://github.com/rtl-buddy/yosys). Put `yosys` on `PATH`:

```bash
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang    # or make config-gcc on Linux
make -j 8
make install
yosys --version
```

`tool: openroad` also needs `openroad` on `PATH` (`openroad -version`). On macOS, follow `tools/openroad/SETUP_OSX.md` in the project template. `rb tool-check --explain yosys` reports what is missing.
