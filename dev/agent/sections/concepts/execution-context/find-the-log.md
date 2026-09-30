## Find the log

Read `<command_root>/rtl_buddy.log`: plain text by default, JSON Lines under `--machine`. For a regression, read the suite's log for test detail and the manifest-root log for the final summary.

Each run's first open of the log truncates it, so the file holds one run. That is why `--run-tag` moves the log into the tagged artefact root: concurrent runs sharing one file would erase each other's record.

Commands that take no lock attach no file log: `rb phys`, `rb cov`, the `rb graph` read verbs, `rb xplr`, and `--list` on any flow command. They report on the console and, under `--machine`, in the JSON result. See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#know-which-commands-write-a-log).
