## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holder's PID, command, start time and host. It takes the lock before opening its `rtl_buddy.log`, so a refused call leaves the holder's log untouched. Listing commands do not lock, and neither do `rb verible`, `rb filelist` and `rb saif`. They run alongside each other and alongside a run that holds the tree. A `rb filelist`, `rb saif` or `rb verible filelist -o` whose output lies inside the artefact tree does lock, since a run holding the tree may be reading that file.

- Wait for the first process to finish, or terminate it if it is stale.
- The lock is released on a clean exit, a tool failure and `Ctrl-C` (exit 130). The kernel also releases it on crash or kill, so the metadata file never needs removal.
- A lock naming this host and a dead process, or a PID since reused by another process, is reclaimed by the next run, which logs `artifact_lock.reclaimed`.
- A lock recorded on another host is never judged by PID. On a shared filesystem, clear a cross-machine lock deliberately.
- The lock covers the whole artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Runs with different artefact roots do not, which is what [`--run-tag`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/#namespace-concurrent-runs) provides. Two runs naming the same tag still contend.
- A tagged `rb pnr` that copies a block abstract or synthesis netlist out of another tree, or runs `--synth` into an untagged tree, holds that tree's lock only for that step, and waits for it rather than failing, logging `artifact_lock.wait`. See [P&R](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#run-several-pr-calls-at-once).
- The lock is host-local. Do not run the same suite from several machines on a shared filesystem without your own coordination.
