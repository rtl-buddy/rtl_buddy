## RTL-MP macro placement

`floorplan.macro-placement: rtl-mp` (default `pack`) hands macros to OpenROAD's hierarchical macro placer, `rtl_macro_placer`, instead of the packer:

```yaml
    floorplan:
      utilization: 0.45
      macro-placement: rtl-mp
```

RTL-MP places macros by connectivity and wirelength and may rotate them. Macros end up `LOCKED` on the track grid, and reports go to `artefacts/<run>/rtlmp/`. Use it for a flat netlist with many memories or when timing depends on macro placement; on the template's sky130hd assembly it gave about 0.8 ns better setup slack than the packer.

- It keeps `placement.macro-halo` around every macro and keeps macros out of every placement blockage, including `soft` and `partial` ones that the packer lets macros sit on.
- [`macro-cell-halo`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#tune-the-process-dependent-steps) and the power grid apply afterwards as usual.
- Setting `macro-anchor` with `rtl-mp` is a configuration error. Its result also changes when the netlist does, so use the packer to pin a macro whose neighbour's pin plan depends on its location.
- `macro-placement` is part of a hardened block's configuration, so switching placer makes the block's abstract stale.
