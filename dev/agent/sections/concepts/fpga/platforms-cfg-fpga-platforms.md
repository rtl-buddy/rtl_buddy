## Platforms: `cfg-fpga-platforms`

Define shared part and board constraints in `cfg-fpga-platforms`, then select the platform from a run:

```yaml
# root_config.yaml
cfg-fpga-platforms:
  - name: zu7ev_board
    part: xczu7ev-ffvc1156-2-e
    board: my-zu7ev-board
    xdc: [constraints/board.xdc]
```

```yaml
# fpga.yaml
runs:
  - name: counter_zu7ev
    model: fpga_counter
    model_path: ../src/models.yaml
    platform: zu7ev_board
    xdc: [constraints/counter_timing.xdc]
```

Platform XDC files are read first. Per-run XDC files extend that set and are read afterward, so later run-level commands can override platform defaults. Platform paths resolve from `root_config.yaml`; run paths resolve from `fpga.yaml`.
