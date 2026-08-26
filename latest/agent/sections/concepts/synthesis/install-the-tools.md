## Install the tools

RTL Buddy validates against the [RTL Buddy Yosys fork](https://github.com/rtl-buddy/yosys):

```bash
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang
make -j 8
make install
yosys --version
```

Use `make config-gcc` on Linux when appropriate. Ensure `yosys` is on `PATH`.

For `tool: openroad`, build OpenROAD and put `openroad` on `PATH`:

```bash
openroad -version
```

On macOS, use the source-build instructions in the project template's `tools/openroad/SETUP_OSX.md`.
