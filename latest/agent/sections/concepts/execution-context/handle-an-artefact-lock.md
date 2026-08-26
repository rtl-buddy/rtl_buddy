## Handle an artefact lock

Every artefact-writing command takes a non-blocking advisory lock on `<artifact_root>/.rtl-buddy.lock`. A second writer to the same tree fails immediately and reports the holding PID, command, and start time.

Wait for the first process to finish or terminate that process if it is stale. The kernel releases the lock on normal exit, crash, or kill; the metadata file itself does not need removal. Listing commands do not take the lock.

The lock covers the entire artefact tree, so different commands anchored to the same directory contend even when they write different subdirectories. Commands using different artefact roots can run concurrently.

This protection is host-local. Do not run the same suite concurrently from multiple machines on a shared filesystem unless the environment provides equivalent coordination.
