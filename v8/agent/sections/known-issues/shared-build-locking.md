## Shared-build locking

An advisory `flock` on `<shared directory>/.rb-build.lock` serialises concurrent processes populating one shared build. A waiting process logs `compile.build_lock_wait` every few minutes.

- On an NFS mount with `nolock`, `local_lock=flock` or `local_lock=all`, the lock is process-local and succeeds without warning, so it does not protect across nodes.
- Delete a shared build tree between runs, never during one; the lock file lives inside it.
- Where the filesystem cannot lock, the run warns `compile.build_lock_unavailable` and compiles unserialised.

Unshared builds have no lock, so do not run such a suite twice at once.
