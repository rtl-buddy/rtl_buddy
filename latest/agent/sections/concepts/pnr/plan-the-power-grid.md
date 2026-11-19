## Plan the power grid

The PDK's `pdn-config` declares the grid for every run. A run can replace it with `pdn-config:` (another Tcl file), or replace its core grid with `pdn:` in YAML. `pdn:` keeps the `pdn-config`'s global connections, voltage domains and macro grids, so it still needs one.

```yaml
    pdn:
      ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
      stripes:
        - {layer: met1, width: 0.48, followpins: true}
        - {layer: met4, width: 1.6, pitch: 20, offset: 5}
        - {layer: met5, width: 1.6, pitch: 20, offset: 5}
```

- Each stripe layer connects to the next unless `connect: [[met1, met4], ...]` lists the pairs.
- A ring must fit between core and die. Only one always-on power domain is supported.
- The PDK's global connections are applied again at the end of every stage, so cells added after `pdngen` get supply connections.

A macro grid with a `-halo` needs the rows cut at least that far around each macro, or `pdngen` fails with `PDN-0008` where a halo reaches a row the macro does not sit on. The tap-cell step cuts them after macro placement. Without a `tapcell-tcl` it runs `cut_rows` with 2 µm, as ORFS does, which suits ORFS's 2 µm halos.

For a wider halo, cut the rows in the PDK's `tapcell-tcl`, for example `cut_rows -halo_width_x 5 -halo_width_y 5` before `tapcell`. Shrinking the halo instead can leave core straps ending next to the macro, blocking its pin escapes.
