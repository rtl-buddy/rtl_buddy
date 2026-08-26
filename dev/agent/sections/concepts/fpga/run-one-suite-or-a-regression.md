## Run one suite or a regression

```bash
rb fpga
rb fpga demo_fpga -c fpga/demo/fpga.yaml
rb fpga demo_fpga --bitstream
rb fpga --list
rb fpga-regression -c ci/fpga_regression.yaml -l 1000
```

Without `--bitstream`, the flow stops after routing and reports; `bitstream` is `null`. With bitstream generation enabled, Vivado downgrades the IP-oriented `NSTD-1` and `UCIO-1` bitgen blockers to warnings immediately before `write_bitstream`. Their original severities remain in `drc.rpt`; board projects should still constrain every pin.

A regression manifest lists `fpga.yaml` suites:

```yaml
rtl-buddy-filetype: fpga_reg_config

fpga-configs:
  - blocks/counter/fpga.yaml
  - blocks/fifo/fpga.yaml
```

Runs above `-l/--reg-level` are SKIP. Machine-mode regression results include the originating suite. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/dev/reference/cli/) for selection and output options.
