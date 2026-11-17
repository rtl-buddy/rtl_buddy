## OpenROAD threads

`threads:` sets OpenROAD's thread count. Unset, rb passes none and OpenROAD uses every hardware thread of the host on 26Q4, one on older versions.

- A count, or `auto`: the CPUs of the Slurm allocation or affinity mask the run is in, 1 outside one, never the host's core count.
- A count above the allocation is clamped with `openroad.threads_capped`.
- Run inside an allocation (`srun -c 8 rb pnr ...`) or use [`--dispatch slurm`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#dispatch-the-stages-to-slurm) to give it CPUs.
- Results can differ slightly between thread counts.

The result's `openroad_threads` gives `requested` and `effective`, the count OpenROAD logged or rb set.

- With `threads:` unset OpenROAD logs no count, so `effective` is null, as is the per-stage `threads` a staged run records in `stage_end` events and the manifest.
- A one-session run records no per-stage `threads`.
- `rb power` and `rb synth`'s OpenROAD stage read `threads:` and report `openroad_threads` the same way.
