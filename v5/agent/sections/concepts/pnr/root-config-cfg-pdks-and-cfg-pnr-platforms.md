## Root config: `cfg-pdks` and `cfg-pnr-platforms`

PDK assets live in `cfg-pdks` (per-process, corners as sub-fields — see the [synthesis page](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/synthesis/#pdk-and-synth-platform-configuration)). `cfg-pnr-platforms` is the P&R-side selector:

```yaml
cfg-pnr-platforms:
  - name: "nangate45_typ"
    pdk: "nangate45"
    corner: "typ"
    cts-buffer: "BUF_X4"
    routing-layers:
      signal: "metal2-metal8"
      clock:  "metal4-metal8"
```

| Field | Description |
|-------|-------------|
| `name` | Referenced by `platform:` in `pnr.yaml` |
| `pdk` | `cfg-pdks` entry name |
| `corner` | Corner from the PDK used for STA; defaults to the first declared corner |
| `cts-buffer` | Standard cell name passed to `clock_tree_synthesis -root_buf` / `-buf_list` |
| `routing-layers.signal` | Layer range for signal routing (e.g. `metal2-metal8`) |
| `routing-layers.clock` | Layer range for clock routing (typically higher metals) |
