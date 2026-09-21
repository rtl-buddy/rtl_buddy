## Artifact locking is per tree and per host

Artifact-writing commands take `<artifact_root>/.rtl-buddy.lock` and fail immediately on same-host contention. The file remains after release; kernel lock state, not file presence, determines whether the tree is locked.

A lock that cannot be taken is re-read before it is reported. If the record names this host and a process that no longer exists — or one whose recorded start time does not match the live process of that pid — the next run reclaims the tree and logs `artifact_lock.reclaimed`. A record written on another host, or one with no host recorded, is never reclaimed by a pid check here; clear it by hand once the other machine is known to be idle.

The lock is intentionally coarse across command families and is not assumed to coordinate different NFS hosts. Dispatched worker jobs skip it because they write planned subdirectories, so do not start another command against a tree with a dispatch run in flight.

A filesystem that cannot `flock` at all (`ENOLCK` on some NFS mounts, a read-only tree) now fails with `cannot lock this artefact tree` rather than claiming another run holds it. Unlike the shared-build lock, the tree lock does not degrade to unlocked.

Give concurrent runs of one suite a [`--run-tag`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/#namespace-concurrent-runs) so each locks its own tree. Two runs naming the same tag still contend, and the lock stays host-local.
