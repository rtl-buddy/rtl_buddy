## Oversized resource groups are split into several arrays

A resource group larger than the cluster's array limit is submitted as several arrays, and `max-jobs-per-array` throttles each one, so peak concurrency is the throttle times the number of slices. With several clusters selected, or no `scontrol` on the submit host, the limit is unknown and sbatch refuses an oversized group; set `cfg-dispatch.max-array-size`. See [Split large groups into several arrays](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#split-large-groups-into-several-arrays).
