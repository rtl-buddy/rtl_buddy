## Running synthesis

Run all syntheses in a config:
```bash
rtl-buddy synth -c synth/sandbox/synth.yaml
```

Run a named synthesis:
```bash
rtl-buddy synth sandbox_sky130 -c synth/sandbox/synth.yaml
```

List syntheses without running:
```bash
rtl-buddy synth --list -c synth/sandbox/synth.yaml
```

Run a synthesis regression:
```bash
rtl-buddy synth-regression -c synth_regression.yaml
```

Run only up to regression level 0:
```bash
rtl-buddy synth-regression -c synth_regression.yaml --reg-level 0
```
