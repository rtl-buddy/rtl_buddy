## Run one suite or a regression

```bash
rb fpga
rb fpga demo_fpga -c fpga/demo/fpga.yaml
rb fpga demo_fpga --bitstream
rb fpga --list
rb fpga-regression -c ci/fpga_regression.yaml -l 1000
```

Without `--bitstream`, the flow stops after routing and reports, and `bitstream` is `null`. When a bitstream is requested, Vivado downgrades the IP-oriented `NSTD-1` and `UCIO-1` bitgen blockers to warnings just before `write_bitstream`. `drc.rpt` keeps their original severities. Board projects should still constrain every pin.

A regression manifest lists `fpga.yaml` suites:

```yaml
rtl-buddy-filetype: fpga_reg_config

fpga-configs:
  - blocks/counter/fpga.yaml
  - blocks/fifo/fpga.yaml
```

Runs above `-l/--reg-level` are SKIP. Machine-mode regression results include the originating suite. Selection and output options are in the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/).

Filelist `+incdir+` entries reach both backends: Vivado's `synth_design` gets them as `-include_dirs` and openXC7's `read_verilog` as `-I`. Each directory resolves against the filelist that declared it.
