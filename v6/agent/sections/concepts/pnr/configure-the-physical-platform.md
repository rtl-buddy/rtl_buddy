## Configure the physical platform

PDK files are defined once under `cfg-pdks`. Select a process and corner for P&R under `cfg-pnr-platforms`:

```yaml
cfg-pnr-platforms:
  - name: nangate45_typ
    pdk: nangate45
    corner: typ
    cts-buffer: BUF_X4
    routing-layers:
      signal: metal2-metal8
      clock: metal4-metal8
```

The PDK entry supplies Liberty, technology and macro LEF, cell GDS, site, and other cell names. See [Synthesis: Configure tools and the PDK](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/#configure-tools-and-the-pdk) and the [root config schema](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml).
