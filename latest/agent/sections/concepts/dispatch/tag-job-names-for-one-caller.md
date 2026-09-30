## Tag job names for one caller

Set `RTL_BUDDY_JOB_TAG` to prefix every Slurm job name an invocation submits with the tag and `:`. A CI system that submits as the same user as developers uses it to find one pipeline's jobs. The tag leads because default `squeue` output shows only the first eight characters of a name.

```bash
RTL_BUDDY_JOB_TAG=ci-1234-1 rb regression -c regression.yaml --dispatch slurm
squeue --me --noheader --format='%i %j' | grep ' ci-1234-1:'
sacct -X --noheader --format=JobID,JobName%80,State | grep ' ci-1234-1:'
```

| Job | Untagged name | Tagged name |
|---|---|---|
| Build | `rb-build-<hash>` | `<tag>:rb-build-<hash>` |
| Verilate | `rb-verilate-<hash>` | `<tag>:rb-verilate-<hash>` |
| Single simulation or elaboration | `rb:<name>` | `<tag>:rb:<name>` |
| Array | `rb:<first>+<N>[/<slice>]` | `<tag>:rb:<first>+<N>[/<slice>]` |

- A tag is 1 to 64 characters from `A-Z a-z 0-9 . _ -`. Any other value fails the command with exit code 2 and `dispatch.job_tag_invalid` before anything is submitted. Unset or empty leaves names untagged.
- Build jobs serialise per user, tag and suite. A tagged CI run and an untagged developer run of one suite do not wait for each other; if they share a build tree, the build lock serialises them instead.
- The tag is independent of `--run-tag`, which never changes a job name, and does not affect [adoption](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#interrupted-runs-warn-cancel-adopt). A `--job-name` in `sbatch-args` replaces a simulation job's whole name, tag included.
- `local-parallel` names no jobs and ignores the variable.
