## Subprocesses

Pass an explicit `cwd` to every external tool. Use the artifact directory unless the command documents another location. Use `run_managed_process()` for long-running tools; reserve `subprocess.run()` for short probes and helpers.
