## Meet the Slurm requirements

Before using `--dispatch slurm`, provide:

- `sbatch`, `squeue`, `sacct`, and `scancel` on the submit host. Run `rb tool-check --explain slurm`.
- A shared filesystem exposing the project, artefacts, and Python environment at identical absolute paths on submit and compute hosts.
- The project's Python environment on compute hosts; workers run `sys.executable -m rtl_buddy`.

The submit process only plans, submits, waits, and collects. Compilation and simulation run on compute nodes.
