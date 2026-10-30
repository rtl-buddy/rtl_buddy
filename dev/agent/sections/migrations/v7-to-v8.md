## v7 to v8

### P&R defaults follow OpenROAD and ORFS

Each default `rb pnr` applies is now the OpenROAD command's own default or the setting ORFS uses, and the flow runs the optimisation steps ORFS runs. Two keys that neither has a default for are required, and the rest of the defaults below changed, so a run that relies on them places, routes and times differently. To keep the v7 flow, set the v7 value:

| Key | v7 default | v8 default | v7 behaviour |
| --- | --- | --- | --- |
| `cfg-pdks` `pin-layers` | `metal3` / `metal2` | None; `rb pnr` needs both | `pin-layers: {horizontal: metal3, vertical: metal2}` |
| `floorplan.utilization` | `0.55` | None; required unless `die-area` and `core-area` are set | `floorplan: {utilization: 0.55}` |
| `floorplan.core-margin` | `2.0` | `1.0`, ORFS's `CORE_MARGIN` | `core-margin: 2.0` |
| `placement.padding` | `1` | `0`, as `global_placement` and ORFS | `placement: {padding: 1}` |
| `placement.macro-cell-halo` | `1.0` | `0`, no blockage | `placement: {macro-cell-halo: 1.0}` |
| `tapcell-tcl` unset | No tap-cell step | `cut_rows`, 2 µm around each placed macro, as ORFS | A `tapcell-tcl` file with no commands |
| `cts-root-buffer` unset, `cts-buffer` a list | The first entry | CTS picks the root from the list, as `clock_tree_synthesis` without `-root_buf` and ORFS | `cts-root-buffer:` the first entry |
| `post-cts-setup-repair` | `false` | `true`: setup, then hold, as ORFS repairs both after CTS | `post-cts-setup-repair: false` |
| `global-route.congestion-iterations` | `20` | `30`, as ORFS | `global-route: {congestion-iterations: 20}` |
| `floorplan-setup-repair` | No step | `true`: setup repair by buffer removal and sizing at the end of the floorplan, as ORFS | `floorplan-setup-repair: false` |
| `placement.improve-placement`, `placement.optimize-mirroring` | No step | `true`: `improve_placement`, then `optimize_mirroring`, after the placement stage's legalization, as ORFS | `placement: {improve-placement: false, optimize-mirroring: false}` |
| `cts-repair-clock-inverters` | No step | `true`: `repair_clock_inverters` before CTS, as ORFS | `cts-repair-clock-inverters: false` |
| `global-route-repair` and `global-route-hold-repair` unset on the platform and the run | No repair after global route | `repair_design`, then setup and hold repair, on global-route parasitics, as ORFS. A written block runs what it sets, as in v7 | `global-route-repair: null` |
| `global-route-antenna-repair`, `detailed-route-antenna-repair` | No antenna repair | `true`: `repair_antennas` after global route, and after detailed route with a detailed route again, as ORFS | `global-route-antenna-repair: false`, `detailed-route-antenna-repair: false` |

A PDK without both `pin-layers` fails every `rb pnr` run on it at setup (`pnr.pin_layers_missing`); one used only by `rb synth` or `rb power` needs nothing. A `pnr.yaml` run without `floorplan.utilization` or `die-area` fails to load. Setup repair needs wire RC, so on a PDK without `layer-rc-tcl` whose technology LEF has no layer RC, such as Nangate45, OpenROAD stops at the floorplan's setup repair with `RSZ-0089`. Set the PDK's `layer-rc-tcl` (see [Set wire RC](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#set-wire-rc)), or turn every setup repair off: `floorplan-setup-repair: false`, `post-cts-setup-repair: false`, and a `global-route-repair` without `setup-margin`, such as `{hold-margin: 0}`. A platform without `cts-buffer` or `routing-layers`, which failed in v7, now runs: CTS picks its buffers from the libraries, and the global router keeps the technology's routing layers. See [Configure the physical platform](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#configure-the-physical-platform).

The hardened-block config digest records the flow's generation, so every abstract hardened with v7 is stale. Harden the blocks again, or pass `--accept-stale` to use them as they are. See [Harden a block](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#harden-a-block).

### RTL-MP is the only built-in macro placer

`rb pnr` places hard macros with OpenROAD's RTL-MP, as ORFS does. rb's macro packer, the v7 default, is gone, with the two keys that steered it, and RTL-MP takes the arguments ORFS passes it. A run that still writes `floorplan.macro-placement`, `floorplan.macro-anchor` or `placement.macro-halo` fails to load with `config.unknown_key_rejected`, whose message says what to do instead.

| Key | v7 | v8 | Migration |
| --- | --- | --- | --- |
| `floorplan.macro-placement` | `pack` (default), rb's packer, or `rtl-mp` | Removed; RTL-MP places the macros | Remove the key. To place macros another way, fix them with `floorplan.macros` locations or place them in a `macro-placement` hook |
| `floorplan.macro-anchor` | `lower-left`, the core corner the packer started from | Removed | Remove the key. Fix the macros that must sit at a corner with `floorplan.macros` locations, or place them in a `macro-placement` hook |
| `placement.macro-halo` | The packer's spacing between macros and from the core edge, default 20 µm; RTL-MP's halo only when set | Removed; `placement.macro-channel` is RTL-MP's minimum channel (`-min_channel_size`), unset by default, as ORFS without `RTLMP_MIN_CHANNEL_SIZE` | A run that set the halo for RTL-MP: set `macro-channel` to twice it, as OpenROAD converts a halo. A run that used RTL-MP without a halo: leave `macro-channel` unset. A packer run: `macro-channel: 40` is the nearest to the packer's 20 µm spacing, or take ORFS's platform value, such as `[80, 80]` on sky130hd |
| `placement.macro-pin-aware-channels` | None | `true`: with a `macro-channel`, RTL-MP trims each channel to the macro pins along it (`-pin_aware_channels`), as ORFS | `placement: {macro-pin-aware-channels: false}` |
| RTL-MP `-target_util` | Not passed; RTL-MP used its own `0.25` | `placement.density`, as ORFS | None; `placement.density` sets both |

Macros land where RTL-MP puts them, by connectivity and possibly mirrored, so a design with macros places, routes and times differently. Also:

- Without a channel, RTL-MP can put macros against each other and the core edge, leaving no routing channel; global routing then fails with `GRT-0116`. Set `placement.macro-channel` on the PDK, such as ORFS's `[80, 80]` on sky130hd. It needs OpenROAD 26Q4 or newer.

- A `floorplan.macros` or `macros-file` entry with an `orientation` and no `location` fails to load, because RTL-MP chooses each orientation. Give it a location, or place the macros in a `macro-placement` hook, which may still use such entries.
- Macros placed by RTL-MP or a `macro-placement` hook are checked against `hard` blockages and core cut-outs, as the packer kept out of `hard` ones. An overlap fails the run (see [Shape the floorplan](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#shape-the-floorplan)).
- A `macro-placement` hook replaces RTL-MP as it replaced the packer, and no longer conflicts with any floorplan key.

See [Place macros](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#place-macros).

### `openroad_threads.effective` is null when `threads:` is unset

With `threads:` unset, rb passes no thread count and OpenROAD picks its own, which it does not log: every hardware thread of the host on 26Q4. The `openroad_threads.effective` field of `rb pnr`, `rb power` and `rb synth` results is then null; v7 reported 1. Set `threads:` to a count or `auto` to know the count. See [OpenROAD threads](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#openroad-threads).
