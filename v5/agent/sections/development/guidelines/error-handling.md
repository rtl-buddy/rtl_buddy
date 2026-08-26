## Error Handling

Fatal config and environment errors should log at ERROR and raise `FatalRtlBuddyError`.
The top-level command exits with code 2.

Per-test setup and filelist failures should become structured test results when the broader command can continue.
Use `FilelistError` for filelist failures caught by `TestRunner`.
Sweep and preproc failures should return a setup-failure string so the suite records `SetupFailResults`.

Do not use process-wide abort patterns for recoverable per-item failures.
