## OpenROAD threads

OpenROAD runs on one thread unless the run asks for more. Reserving CPUs from a scheduler does not change that. Set `threads:` on the run:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: 8        # or: auto
```

- Unset gives 1 thread. A positive integer gives that many. `auto` gives the CPUs of the allocation the run is in, or 1 with none; never the host's core count.
- Zero, negative numbers, booleans, quoted numbers and other strings fail configuration loading.

`rb pnr` is not dispatched. Run it inside an allocation (`srun -c 8 rb pnr ...` or an `sbatch` script) to give OpenROAD CPUs. The allocation is `SLURM_CPUS_PER_TASK` (else `SLURM_CPUS_ON_NODE`) in a Slurm job, or a smaller CPU affinity mask on Linux. A container CPU quota (`docker --cpus`) is not visible.

A count above the allocation is clamped to it with an `openroad.threads_capped` warning naming both numbers. Outside an allocation an explicit count is used as given.

`--machine` output carries `openroad_threads` with the `requested`, `effective` and allocation values. The same rules apply to [`rb power`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/power/) and the OpenROAD stage of [`rb synth`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/synthesis/). Results are not guaranteed to be bit-identical across thread counts; compare DRC and slack if it matters.
