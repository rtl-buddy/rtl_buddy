## Install OpenROAD

OpenROAD 25Q1 or newer must be on `PATH` or configured under `cfg-power-tools`. Only `tool: openroad` is supported; other tools report `SKIP`.

The run's `platform` names a `cfg-pnr-platforms` entry that supplies the PDK and Liberty corner; see [Place-and-Route: Configure the physical platform](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#configure-the-physical-platform). A platform with `corners:` analyses every listed corner in one session and one SDC mode; see [Place-and-Route: Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#sign-off-at-several-corners) and [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#rb-power-times-every-corner-in-one-sdc-mode).
