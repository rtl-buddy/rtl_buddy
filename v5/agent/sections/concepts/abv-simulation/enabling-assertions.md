## Enabling assertions

Set `assertions: true` on a test in `tests.yaml`:

```yaml
tests:
  - name: smoke_with_sva
    desc: "smoke test with SVA assertions compiled in"
    reglvl: 0
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    assertions: true
```

When `assertions: true` and the builder is Verilator, `rb test` appends `--assert` and `--coverage-user` to the Verilator compile command. The flags are idempotent — already-configured values in `root_config.yaml` builder opts are not duplicated.

For non-Verilator builders the flag is currently a no-op — but not a silent one: the run logs a `compile.assertions_not_verilator` WARNING naming the simulator family, so a misconfigured non-Verilator run is visible rather than ignored. VCS/Xcelium SVA enablement is a follow-up.
