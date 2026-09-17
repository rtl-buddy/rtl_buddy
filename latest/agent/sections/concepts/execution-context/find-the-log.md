## Find the log

Read `<command_root>/rtl_buddy.log`. It is plain text by default and JSON Lines under `--machine`. For regressions, inspect the relevant suite log for test details and the manifest-root log for the final summary.

The log belongs to the flow that wrote it. A file log is opened for writing and a process's first open of it truncates it, so the file holds one run and not an accumulation of every run before it. The commands that take no lock also attach no file log — `rb phys`, `rb cov`, the `rb graph` read verbs, `rb xplr`, and `--list` on any flow command — because they write nothing else, and opening the log would destroy the record of the run being read about. They report on the console and, under `--machine`, in the JSON result on stdout. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/v6/agents/#know-which-commands-write-a-log).
