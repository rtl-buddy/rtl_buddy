## Choose a backend

| Backend | Execution | Concurrency | Resources enforced | Reservation advice |
|---|---|---|---|---|
| `local` | Current process | 1 | No | No |
| `local-parallel` | Subprocesses on this host | `--jobs` or `cfg-dispatch.jobs` | No | No |
| `slurm` | Cluster jobs | Per-array throttle | Yes | From `sacct` |

- `local` is the default. `rb test` dispatches only when `--dispatch` is given; it does not inherit `cfg-dispatch.backend`.
- `rb test` takes the same name list or regex filter locally and under dispatch. See [Run tests](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/tests/#run-tests).
- Of the early stops, dispatch takes only `-E comp`, which runs the build jobs and no simulation. See [Check that every build compiles](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/dispatch/#check-that-every-build-compiles).
- Dispatch implies [`--share-build`](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/tests/#sharing-compiled-builds-across-tests).
- `rb pnr --dispatch slurm` runs each stage of a P&R run as a chained job, sized by the run's `resources:` over `cfg-dispatch.resources`. See [Dispatch the stages to Slurm](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/pnr/#dispatch-the-stages-to-slurm).
