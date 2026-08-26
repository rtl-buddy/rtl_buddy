## Slurm memory advice depends on accounting samples

rtl_buddy requests one-second task accounting unless `sbatch-args` already sets `--acctg-freq`. Memory advice is suppressed when the longest run ends within the active sampling interval because `MaxRSS` is unreliable; time and CPU advice remain available.

Right-sizing suggestions also have fixed five-minute and 128 MB floors and require at least 25% savings. Very small reservations can therefore produce no reduction advice even when utilization is low.
