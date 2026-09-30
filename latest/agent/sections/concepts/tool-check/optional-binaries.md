## Optional binaries

A tool may list `Optional binaries (not required; not detected as this tool)`, each with what it buys. They never change a tool's status: a host with `scontrol` but no `sbatch` reports slurm `missing`.

Slurm's `scontrol` is the main case:

- Without it, dispatch cannot read the cluster's array limits. Set `cfg-dispatch.max-array-size` and, if the cluster caps tasks per array lower, `cfg-dispatch.max-array-tasks`.
- Without `scontrol update`, simulation jobs wait for the whole build job instead of starting as soon as their build finishes. The call runs on the compute node, so a passing check on the submit host does not prove it works there.
