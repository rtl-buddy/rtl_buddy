## Plan the power grid

The PDK's `pdn-config` declares the grid for every run. A run can replace it:

- `pdn-config:` on the run sources another Tcl file instead.
- `pdn:` on the run declares the core grid in YAML and replaces the core grid of the pdn-config in effect, keeping its global connections, voltage domains and macro grids. It still needs a pdn-config.

```yaml
    pdn:
      ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
      stripes:
        - {layer: met1, width: 0.48, followpins: true}
        - {layer: met4, width: 1.6, pitch: 20, offset: 5}
        - {layer: met5, width: 1.6, pitch: 20, offset: 5}
```

By default each stripe layer connects to the next; `connect: [[met1, met4], ...]` lists the pairs instead. A ring must fit between core and die. Only one always-on power domain is supported. The PDK's global connections are applied again at the end of every stage, so cells added after `pdngen` get supply connections.

A macro grid in the `pdn-config` with a `-halo` needs the standard-cell rows cut at least that far around each macro: `pdngen` fails with `PDN-0008` when a halo reaches a row the macro does not sit on. rb does not cut rows around macros, and its packer puts a macro's bottom edge on a row boundary. Cut them in the PDK's `tapcell-tcl`, which runs after macro placement and before the grid, for example with `cut_rows -halo_width_x 2 -halo_width_y 2` for a 2 µm halo, as ORFS's tap-cell step does. Shrinking the halo instead can leave the core straps ending next to the macro, where they block its pin escapes.
