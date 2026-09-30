## Split large groups into several arrays

Slurm refuses an array larger than `MaxArraySize` or `SchedulerParameters=max_array_tasks`. rtl_buddy reads both from `scontrol show config` and splits a larger group into arrays of at most `min(max_array_tasks, MaxArraySize - 1)` elements.

- Each slice logs under `slice-N/` in the run's dispatch directory and has a `/N` job-name suffix. Summary, cancellation, and advice treat the slices as one group.
- `cfg-dispatch.max-array-size` and `max-array-tasks` override the probed values independently. Either alone is enough to slice.
- If neither limit is known, the group is submitted whole. If sbatch refuses it, or refuses an array within every known limit, the error names the field to set or lower. A cluster can enforce a ceiling it does not report.
