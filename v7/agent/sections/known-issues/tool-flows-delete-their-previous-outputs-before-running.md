## Tool flows delete their previous outputs before running

`rb cdc`, `rb synth`, `rb fpga`, `rb pnr` and `rb power` remove the outputs they are about to write before invoking the tool, and `rb hub` clears the `view.json` and domain map it caches under `.rtl-buddy/cache/`. A failed rerun therefore leaves no output instead of the previous run's. Copy out anything you want to compare first.

- A failed `rb synth` leaves no netlist, so `rb pnr` and `rb power` tell you to run `rb synth` first. `rb fpga` without `--bitstream` removes a previously built `<top>.bit`.
- A rerun that fails early, on a filelist or config error, also leaves nothing. With the backend tool missing, only `rb synth` and `rb pnr` still clear.
- A previous output that cannot be deleted (permissions, or a directory where the file belongs) fails the run with a fatal error. Fix the permissions or remove it by hand.
- A `pnr` run failed by `gds-mode: strict` removes the GDS, PNG and report but keeps the routed DEF, netlist, SDC and ODB.

Runs of different commands with the same name share one artifact directory. Give an FPGA run and a power run different names; both write `power.rpt`.
