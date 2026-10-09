## Plan the power grid per run

The PDK's `pdn-config` declares the power grid for every run on it. A run can replace or reshape it:

- **`pdn-config:`** on the run is a Tcl file, relative to `pnr.yaml`, sourced instead of the PDK's. It follows the same contract: declare the grid, never call `pdngen`.
- **`pdn:`** on the run declares the core grid in YAML. It replaces the core grid of the pdn-config in effect (the run's or the PDK's), which must exist. Everything else in that file still runs: global connections, voltage domains and macro grids.

```yaml
    pdn:
      ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
      stripes:
        - {layer: met1, width: 0.48, followpins: true}
        - {layer: met4, width: 1.6, pitch: 20, offset: 5}
        - {layer: met5, width: 1.6, pitch: 20, offset: 5}
      # connect: [[met1, met4], [met4, met5]]   # default: each stripe layer to the next
```

| Key | Meaning |
| --- | --- |
| `ring` | Optional core ring: `layers` `[horizontal, vertical]`, `width`, `spacing` between the power and ground rings, and `offset` from the core edge, all in microns (`add_pdn_ring -core_offsets`). It needs `offset + 2 x width + spacing` of room between core and die, which is checked against `core-margin` or the explicit areas when the run loads. With a ring, every stripe extends to it. |
| `stripes` | The core grid's stripes, bottom layer first. A `followpins: true` entry gives the rails along the standard-cell rows and takes only `width`. Any other stripe needs `pitch`, and takes optional `offset` and `spacing` (`add_pdn_stripe`). |
| `connect` | Optional `[lower, upper]` layer pairs to join with vias (`add_pdn_connect`). By default each stripe layer is joined to the next one listed. A ring on layers that also carry stripes needs nothing more; put other ring layers in `connect`. |

While it sources the pdn-config, the flow skips each `define_pdn_grid` that is neither `-macro` nor `-existing`, and that grid's stripes, rings and connects, and logs `pdn: core grid '<name>' from the pdn-config replaced by the run's pdn: block`. The run's grid keeps the skipped grid's name, voltage domains, `-pins` layers and starting net; without one it is `rb_core` on OpenROAD's default domain. Macro grids that connect to the core straps, as the template's `-grid_over_boundary` grids do, keep working only if `pdn:` still has straps on the layers they connect to.

- `pdn:` with no pdn-config at all (for example on Nangate45) fails at setup: the global connections and voltage domain have to come from Tcl.
- Malformed entries fail at load: a stripe without `pitch`, two stripes wider than their pitch, `pitch` on a followpins entry, a ring without two layers, or a ring too wide for the core-to-die gap.
- Switchable power domains are not supported: they need power-intent (UPF) support, which `rb pnr` does not have. Use one always-on domain.
- `pdn-config` and `pdn` are part of a hardened block's configuration when set.
