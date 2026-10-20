## Run P&R

```bash
rb pnr --list -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png --gds-mode strict
```

`--png` and `--gds-mode` imply `--gds`. KLayout runs after a successful OpenROAD run. In the default `preview` mode a KLayout failure logs a warning and leaves the P&R verdict unchanged. In `strict` mode an undelivered export fails the run; see [Stream-out completeness](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-completeness).
