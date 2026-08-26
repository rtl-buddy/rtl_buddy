## Shared-build dependency tracking varies by simulator

Verilator reports consumed headers, library files, its standard includes, and its binary, so changes invalidate the shared-build stamp. VCS and Icarus do not report equivalent dependency data; editing a header reached only through `+incdir+` or `-y` can reuse a stale build.

Ambient environment variables and undeclared tool inputs are not tracked. Force a rebuild by deleting the specific shared directory under `artefacts/.shared-builds/` or the test's `rb-compile-stamp.json`, or run without `--share-build`. `compile.build_dep_changed` explains detected invalidation.
