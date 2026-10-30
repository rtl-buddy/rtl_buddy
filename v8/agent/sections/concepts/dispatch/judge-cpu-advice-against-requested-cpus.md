## Judge cpu advice against requested cpus

Cpu efficiency is measured against the cpus requested, not allocated, so a partition that allocates whole cores does not make single-threaded runs look half used. The table shows `4 (8 allocated)` when the two differ. `mem` and `time` advice use the scheduler's own figures.
