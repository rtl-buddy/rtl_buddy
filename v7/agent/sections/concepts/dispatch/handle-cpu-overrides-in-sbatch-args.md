## Handle cpu overrides in sbatch-args

`cfg-dispatch.sbatch-args` is appended after the generated reservation flags, so these options change what jobs actually request:

- `-c` / `--cpus-per-task`
- `-n` / `--ntasks`, `--ntasks-per-node`, `-N` / `--nodes`
- A GPU count with `--ntasks-per-gpu` and no `--ntasks`

`SBATCH_NTASKS`, `SBATCH_NTASKS_PER_NODE`, and `SBATCH_NODES` count too; a command-line option beats its variable. `SBATCH_CPUS_PER_TASK`, `--exclusive`, `--overcommit`, and placement options do not.

While an override is in force, `cpus` findings point at `cfg-dispatch.sbatch-args` (`path: env` when only the environment set it) and say which field it superseded. `suggested` is the whole-job cpu count, and the note says which fields can take it:

```
sbatch-args `--cpus-per-task=4` sets this job's cpu request, superseding
tests[name=wr_single].resources.cpus; change it there. Suggested value is
the whole-job cpu count.

`--ntasks=4` multiplies this job's cpu request: the generated --cpus-per-task
from tests[name=wr_single].resources.cpus still applies, so the request is 8
per task x 4 tasks. Suggested value is the whole-job cpu count — lower
tests[name=wr_single].resources.cpus, the task count in sbatch-args, or both;
no single one of them takes it.

sbatch-args supersedes tests[name=wr_single].resources.cpus: `--ntasks=4` and
`--cpus-per-task=2` set this job's cpu request together. Suggested value is the
whole-job cpu count — decompose it across them per sbatch's own precedence; no
single one of them takes it.
```

A test whose retries ran under different cpu requests gets no `cpus` row (`rightsize.cpus_advice_withheld`). A direct `--cpus-per-task` in `sbatch-args` also removes the compile `cpus` floor.
