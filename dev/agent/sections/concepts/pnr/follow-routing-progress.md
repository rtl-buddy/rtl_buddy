## Follow routing progress

`pnr.log` is OpenROAD's output, written as it happens, so `tail -f artefacts/<run>/pnr.log` follows a running flow.

- Every global route runs with `-verbose`. After it, `pnr.log` has the per-layer resource and usage table (`[INFO GRT-0096] Final congestion report:`), which shows the congestion margin of a passing run. With `global-route-hold-repair`, the incremental reroute prints the table again.
- When global routing overflows, each overflowing tile, with its capacity, usage and nets, goes to `congestion.rpt`, in the DRC-report format the OpenROAD GUI's DRC viewer loads. A route that fails with `GRT-0116` keeps it.
- Detailed routing logs each optimization iteration (`[INFO DRT-0195] Start 1st optimization iteration.`), its progress (`Completing 30% with 88 violations.`) and its result (`[INFO DRT-0199] Number of violations = 16.`), so a converging route can be told from a stalled one. Set `detailed-route-verbose:` on the run to change the level: `0` silences the iterations; higher levels add more router detail.

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    detailed-route-verbose: 0    # default 1
```
