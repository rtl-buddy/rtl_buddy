## Resolve the manifest

An explicit config wins:

```bash
rb regression --reg-config path/to/regression.yaml
```

Otherwise RTL Buddy checks, in order:

1. `./regression.yaml` in the invocation directory.
2. `cfg-rtl-reg.reg-cfg-path` in `root_config.yaml`.

Other flow regressions follow the same order: explicit `-c`, then `./<flow>_regression.yaml`, then `cfg-rtl-reg.<flow>-reg-cfg-path`. Declare non-root flow manifests in `cfg-rtl-reg` so graph discovery can find them.
