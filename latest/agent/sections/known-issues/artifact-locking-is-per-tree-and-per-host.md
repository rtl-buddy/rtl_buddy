## Artifact locking is per tree and per host

Artifact-writing commands take `<artifact_root>/.rtl-buddy.lock` and fail at once if another process on the same host holds it.

- A lock left by a dead process on this host is reclaimed by the next run with the warning `artifact_lock.reclaimed`. A lock from another host is never reclaimed; clear it by hand once that machine is idle.
- The lock does not coordinate different NFS hosts. Dispatched worker jobs skip it, so do not start another command against a tree with a dispatch run in flight.
- A filesystem that cannot `flock` fails with `cannot lock this artefact tree`.

Give concurrent runs of one suite a [`--run-tag`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/#namespace-concurrent-runs) so each locks its own tree.
