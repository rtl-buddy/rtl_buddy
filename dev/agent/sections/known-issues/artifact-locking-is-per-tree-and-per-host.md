## Artifact locking is per tree and per host

Artifact-writing commands take `<artifact_root>/.rtl-buddy.lock` and fail immediately on same-host contention. The file remains after release; kernel lock state, not file presence, determines whether the tree is locked.

The lock is intentionally coarse across command families and is not assumed to coordinate different NFS hosts. Dispatched worker jobs skip it because they write planned subdirectories, so do not start another command against a tree with a dispatch run in flight.
