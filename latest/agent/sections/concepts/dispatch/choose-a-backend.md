## Choose a backend

| Backend | Execution | Concurrency | Resources enforced | Reservation advice |
|---|---|---|---|---|
| `local` | Current process | 1 | No | No |
| `local-parallel` | Subprocesses on this host | `--jobs` or `cfg-dispatch.jobs` | No | No |
| `slurm` | Cluster jobs | Per-array throttle | Yes | From `sacct` |

- `local` is the default. `rb test` dispatches only when `--dispatch` is given; it does not inherit `cfg-dispatch.backend`.
- `rb test` takes the same name list or regex filter locally and under dispatch. See [Run tests](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#run-tests).
- Dispatch cannot be combined with `--early-stop`.
- Dispatch implies [`--share-build`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#sharing-compiled-builds-across-tests).
