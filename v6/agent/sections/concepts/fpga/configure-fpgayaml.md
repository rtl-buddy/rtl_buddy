## Configure `fpga.yaml`

Each run references a model and either a part or a reusable platform:

```yaml
rtl-buddy-filetype: fpga_config

runs:
  - name: demo_fpga
    desc: Counter on a ZU7EV
    tool: vivado
    model: fpga_counter
    model_path: ../src/models.yaml
    part: xczu7ev-ffvc1156-2-e
    xdc: [constraints/clocks.xdc]
    reglvl: 1000
    require-timing-met: true
```

`model_path` and XDC paths are relative to `fpga.yaml`. The model name is the synthesis top. `part` and `platform` are mutually exclusive; naming both is an exit-2 configuration error. Expected-failure fields follow [Expected Failures](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/expected-failures/).

`require-timing-met` defaults to false. A completed route that misses timing therefore passes the flow while reporting `timing_met: false`, negative slack, and failing paths. Set it true when timing closure is a regression gate; the failing result still includes timing metrics. A backend that reports no timing result is not failed by this option.
