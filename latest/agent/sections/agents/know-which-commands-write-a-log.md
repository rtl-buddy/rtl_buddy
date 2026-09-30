## Know which commands write a log

Commands that run a flow (`test`, `regression`, `synth`, `power`, and so on) write events to `<command_root>/rtl_buddy.log`. A process's first open truncates the file, so the log holds only the latest run.

Read commands write no file log: `rb phys`, `rb cov`, the `rb graph` read verbs (`query`, `path`, `explain`), `rb xplr`, and `--list` on any flow command. Writing one would truncate the log of the flow they report on. Their events go to stderr and their result goes to stdout as JSON.

To debug a run, read the log of the flow that produced the artefacts, not of the read command that reported them.
