## Key fields

**`cfg-platforms`**

Maps the current OS (detected via `uname`) to a builder and Verible config. `rtl_buddy` picks the first platform entry whose `unames` list contains the output of `uname`.

**`cfg-rtl-builder`**

Defines simulation tool configurations. Each entry has:

- `builder`: simulator executable name (`verilator`, `vcs`, etc.)
- `builder-simv`: path to the compiled simulation binary
- `sim-rand-seed` / `sim-rand-seed-prefix`: default seed value and the plusarg prefix used to pass it
- `builder-opts`: named compile-time and run-time option sets, selected by builder mode

**`cfg-verible`**

Defines Verible tool configurations for lint and syntax checks.

**`cfg-rtl-reg`**

Sets the default path to `regressions.yaml` used by `rtl-buddy regression` when `--reg-config` is not specified.
