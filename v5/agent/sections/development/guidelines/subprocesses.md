## Subprocesses

Every external tool invocation should pass an explicit `cwd`.
Use the command's artifact directory unless the command has a documented reason to run elsewhere.

Use `run_managed_process()` for long-running or tool-managed subprocesses so cleanup, timeout handling, and signal behavior stay consistent.
Plain `subprocess.run()` is acceptable only for short probes or helpers where lifecycle management is not needed; document that choice when it is not obvious.
