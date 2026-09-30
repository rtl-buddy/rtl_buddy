## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holder's PID, command, start time and host. Listing commands do not lock.

- Wait for the first process to finish, or terminate it if it is stale.
- The lock is released on a clean exit, a tool failure and `Ctrl-C` (exit 130). The kernel also releases it on crash or kill, so the metadata file never needs removal.
- A lock naming this host and a dead process, or a PID since reused by another process, is reclaimed by the next run, which logs `artifact_lock.reclaimed`.
- A lock recorded on another host is never judged by PID. On a shared filesystem, clear a cross-machine lock deliberately.
- The lock covers the whole artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Runs with different artefact roots do not, which is what [`--run-tag`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#namespace-concurrent-runs) provides. Two runs naming the same tag still contend.
- The lock is host-local. Do not run the same suite from several machines on a shared filesystem without your own coordination.
