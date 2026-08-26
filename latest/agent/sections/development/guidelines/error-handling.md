## Error Handling

Fatal config and environment errors should log at ERROR and raise `FatalRtlBuddyError`.
The top-level command exits with code 2.

Convert recoverable per-item failures into structured results. Use `FilelistError` for filelist failures caught by `TestRunner`, and return setup-failure strings from sweep or preproc failures so the suite records `SetupFailResults`.
