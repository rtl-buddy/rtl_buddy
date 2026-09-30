## Run a whole hierarchy

`rb pnr` with no run name runs every entry in the `pnr.yaml`, each block's run before the runs that consume it. Without `blocks:`, runs go in file order.

- A block whose `pnr-path` is another `pnr.yaml` is pulled in ahead of its consumer and writes to its own suite's `artefacts/`.
- A run whose block failed is not attempted. It reports `FAIL` with `fail_stage: blocked` and a `blocked_by` list, even under `xfail`, and blocking propagates up the hierarchy. Independent runs still run.
- A block skipped by `-l` does not block its consumer, which uses the abstract already published.
- A cycle in `blocks:` (`pnr blocks: cycle: a -> b -> a`), a block naming a run its `pnr.yaml` does not define, a missing `pnr-path`, or two runs that write the same `artefacts/<run>` directory stop the command before anything runs.
