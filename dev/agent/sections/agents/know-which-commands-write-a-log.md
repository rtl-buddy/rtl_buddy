## Know which commands write a log

Flow commands (`test`, `regression`, `synth`, `power`, and so on) write `<command_root>/rtl_buddy.log`, replacing the previous run's log.

Read commands (`rb phys`, `rb cov`, `rb graph query|path|explain`, `rb xplr`, `rb filelist`, `rb saif`, `rb verible`, and flow `--list`) preserve that log and take no artefact-tree lock; `rb filelist`, `rb saif` and `rb verible filelist -o` writing into `artefacts/` lock it like a flow command. They send events to stderr and JSON results to stdout. Debug the flow using the log that produced its artefacts.
