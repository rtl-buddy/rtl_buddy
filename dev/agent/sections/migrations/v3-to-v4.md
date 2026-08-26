## v3 to v4

Replace `cfg-synth-libs` with reusable PDK data plus flow-specific platform selectors:

```yaml
# v3
cfg-synth-libs:
  - name: nangate45_typ
    path: pdk/.../typical.lib
    lef-paths: [...]

# v4
cfg-pdks:
  - name: nangate45
    corners: { typ: pdk/.../typical.lib }
    tech-lef: pdk/.../tech.lef
    macro-lef: pdk/.../cells.lef
cfg-synth-platforms:
  - { name: nangate45_typ, pdk: nangate45, corner: typ }
```

In `synth.yaml`, replace `libraries: [name]` with `platform: name`. Add `cfg-pnr-platforms` only for `rb pnr`. API users must replace `get_synth_lib_cfg` with `get_synth_platform_cfg`; use `get_pdk_cfg` and `get_pnr_platform_cfg` for the new layers. See [Synthesis](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/) and [Place-and-Route](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/).
