## Slurm memory advice depends on accounting samples

rtl_buddy requests one-second task accounting unless `sbatch-args` sets `--acctg-freq`. When the longest run ends within the sampling interval, `MaxRSS` is unreliable and memory advice is suppressed. Reduction advice needs at least 25% savings and floors of five minutes and 128 MB, so a small reservation gets none.
