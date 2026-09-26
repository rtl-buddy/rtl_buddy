## Install OpenROAD

OpenROAD 25Q1 or newer must be on `PATH` or configured under `cfg-power-tools`. Power runs currently support only `tool: openroad`; unsupported tools report `SKIP`.

The selected `cfg-pnr-platforms` entry supplies the PDK and Liberty corner. A platform with `corners:` analyses every listed corner in one session; see [Place-and-Route: Sign off at several corners](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#sign-off-at-several-corners).
