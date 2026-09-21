## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holding PID, command, start time, and host.

Wait for the first process to finish or terminate that process if it is stale. The lock is released on the way out of every run — a clean exit, a tool failure, or a `Ctrl-C` during a long flow, which now exits 130 — and the kernel releases it anyway on crash or kill; the metadata file itself does not need removal. Listing commands do not take the lock.

A stale lock is reclaimed rather than waited on. When the lock cannot be taken, the record is re-read: if it names this host and a dead process (or a pid that has since been reused by a different process), the next run replaces the lock file under a second `.reclaim` flock that serialises reclaimers, takes the tree, and logs `artifact_lock.reclaimed`. Nothing recorded on another host is judged by a pid here, so a cross-machine lock on a shared filesystem still has to be cleared deliberately.

The lock covers the entire artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Commands using different artefact roots can run concurrently — which is what [`--run-tag`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/#namespace-concurrent-runs) gives two runs of the same suite. Two runs naming the same tag still contend.

This protection is host-local. Do not run the same suite concurrently from multiple machines on a shared filesystem unless the environment provides equivalent coordination.
