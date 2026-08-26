## Run P&R

```bash
rb pnr --list -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
```

`--png` implies `--gds`. RTL Buddy invokes KLayout after a successful OpenROAD run. KLayout conversion failures produce warnings but do not change the P&R verdict; use the OpenROAD timing and DRC results as the run outcome.
