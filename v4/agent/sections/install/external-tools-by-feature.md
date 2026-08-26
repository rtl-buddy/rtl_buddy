## External tools by feature

| Command / feature | Integration type | Curated tools | Sub-deps and notes |
|---|---|---|---|
| `rb test`, `rb randtest`, `rb regression` | Pluggable | Verilator, VCS (Icarus on the roadmap) | Install the `lcov` package in your OS for LCOV / HTML coverage export from Verilator runs. |
| `rb verible` | Integrated tool | Verible | `brew tap chipsalliance/verible && brew install verible` on macOS; or see [Verible releases](https://github.com/chipsalliance/verible/releases). |
| Coverview packaging (under `rb regression`) | Integrated tool | Antmicro [Coverview](https://github.com/antmicro/coverview) | Install the `info-process` package in your OS via Coverview's own setup for full package generation. |
| `rb synth`, `rb synth-regression` | Pluggable | `yosys`, `openroad` | `yosys` is required (the [rtl-buddy/yosys fork](https://github.com/rtl-buddy/yosys), see below); `openroad` is required only when `tool: openroad`. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/synthesis/). |
| `rb pnr` | Integrated tool | OpenROAD ≥ `25Q1` | Optional: `klayout` for `--gds` / `--png` streamout and rendering. See [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/pnr/). |
| `rb cdc`, `rb cdc-regression` | Integrated tool | [rtl-buddy-cdc](https://github.com/rtl-buddy/rtl-buddy-cdc) | SpyGlass support is on the roadmap — tracked in [issue #85](https://github.com/rtl-buddy/rtl_buddy/issues/85). |
| `rb wave` | Integrated tool | Surfer (rtl-buddy fork, `rtl-buddy` branch) | nvim for full annotation round-trip; any editor configurable via `editor-cmd` for one-way "open at line". Vaporview / VS Code support is on the roadmap — tracked in [issue #84](https://github.com/rtl-buddy/rtl_buddy/issues/84). See [Waveform Viewer](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/wave/). |

### Forks required

rtl_buddy currently validates against two forks rather than upstream:

- **Surfer** — required. Use the [`rtl-buddy/surfer`](https://github.com/rtl-buddy/surfer) repo, branch `rtl-buddy`. Mainline Surfer works for basic FST viewing but does not support the WCP signal-value annotation features `rb wave` relies on.
- **Yosys** — required. Use the [`rtl-buddy/yosys`](https://github.com/rtl-buddy/yosys) repo, which tracks upstream with rtl-buddy-specific patches.

Build instructions live on the respective concept pages: [Surfer build](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/wave/#surfer-build) and [Installing Yosys](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/synthesis/#installing-yosys).
