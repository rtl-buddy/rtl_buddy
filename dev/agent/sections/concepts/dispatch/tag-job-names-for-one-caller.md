## Tag job names for one caller

Set `RTL_BUDDY_JOB_TAG` to put a tag and `:` in front of every Slurm job name an invocation submits. A CI system that submits under the same user as developers uses it to find all jobs of one pipeline run with `squeue` or `sacct`, and to tell them from the user's own jobs: the tag leads because default `squeue` output shows only the first eight characters of a name.

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

Unset or empty leaves every name untagged. A tag is 1 to 64 characters from `A-Z a-z 0-9 . _ -`. Any other value — whitespace, a `,` (which `squeue`, `sacct` and `scancel` split `--name` on), `:` or `|` — fails the command with exit code 2 and `dispatch.job_tag_invalid` before anything is submitted. Since a tag cannot contain `:`, the first `:` in a name ends it. The `local-parallel` backend names no jobs and ignores the variable.

The tag is part of the build and verilate jobs' identity, so `--dependency=singleton` dedup is scoped per user, tag and suite. Runs of one suite under the same tag, or both untagged, queue their build jobs behind each other; a tagged CI run and an untagged developer run do not, and if they write one shared-build tree the build lock inside the job serialises them instead. `dispatch.build_submitted`, `dispatch.build_job_deduped` and the dedup probe all carry the tagged name.

The tag is independent of `--run-tag`, which namespaces the artefact tree and never changes a job name. [Interrupted-run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#interrupted-runs-warn-cancel-adopt) adoption matches jobs through the run manifest and job ids, so a tag does not affect it. A `--job-name` in `cfg-dispatch.sbatch-args` still replaces a simulation job's whole name, tag included.
