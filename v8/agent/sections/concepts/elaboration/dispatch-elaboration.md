## Dispatch elaboration

`rb elab` dispatches only with `--dispatch`. `rb elab-regression` also honors `cfg-dispatch.backend`. A profile's `resources` layer over `cfg-dispatch.resources`, and `cpus` is both the scheduler request and the pyslang worker thread count.

- Slurm enforces the memory and time reservations.
- Local-parallel passes `cpus` to pyslang and limits concurrency with its process pool. It does not enforce memory or time.
