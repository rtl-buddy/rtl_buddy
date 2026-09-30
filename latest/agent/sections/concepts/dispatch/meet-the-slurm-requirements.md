## Meet the Slurm requirements

Before using `--dispatch slurm`, provide:

- `sbatch`, `squeue`, `sacct`, and `scancel` on the submit host, plus `scontrol` for the array-size probe. Check with `rb tool-check --explain slurm`.
- A shared filesystem that exposes the project, artefacts, and Python environment at identical absolute paths on submit and compute hosts.
- The project's Python environment on compute hosts. Workers run `python -m rtl_buddy` from the interpreter that started the run.
