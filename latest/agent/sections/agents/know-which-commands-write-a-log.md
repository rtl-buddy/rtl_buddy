## Know which commands write a log

Flow commands (`test`, `regression`, `synth`, `power`, and so on) write `<command_root>/rtl_buddy.log`, replacing the previous run's log.

Read commands (`rb phys`, `rb cov`, `rb graph query|path|explain`, `rb xplr`, and flow `--list`) preserve that log. They send events to stderr and JSON results to stdout. Debug the flow using the log that produced its artefacts.
