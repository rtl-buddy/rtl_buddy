## Install the tools

rtl_buddy is validated against the `rtl-buddy-0.71` branch of the [RTL Buddy Yosys fork](https://github.com/rtl-buddy/yosys): Yosys 0.71 plus SystemVerilog frontend fixes that stock Yosys lacks. [rtl-buddy-tools](https://github.com/rtl-buddy/rtl-buddy-tools) builds it together with a matching yosys-slang plugin and the formal solvers, at one pinned set for macOS and Linux.

To build Yosys on its own, you need a C++20 compiler, CMake 3.28 or newer, bison 3.6 or newer, and flex 2.6 or newer. Yosys's optional Tcl, readline and libffi support is found only through `pkg-config`: without `pkg-config`, the build succeeds but leaves all three out.

Build and install it to `~/.local`, then put that on `PATH`:

```bash
git clone --recursive -b rtl-buddy-0.71 https://github.com/rtl-buddy/yosys.git
cd yosys
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build -j 8
cmake --install build
export PATH="$HOME/.local/bin:$PATH"
yosys -V
```

`yosys -V` reports `Yosys 0.71+`. Add the `export` line to your shell profile to keep `yosys` on `PATH`.

`tool: openroad` also needs `openroad` on `PATH` (`openroad -version`). On macOS, follow `tools/openroad/SETUP_OSX.md` in the project template. `rb tool-check --explain yosys` reports what is missing.
