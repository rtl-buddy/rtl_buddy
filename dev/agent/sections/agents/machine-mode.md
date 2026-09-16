## Machine mode

Pass `--machine` before the subcommand:

```bash
rb --machine test basic
rb --machine regression -c regression.yaml
```

Machine mode:

- writes `rtl_buddy.log` as JSON Lines for the commands that write one at all;
- disables Rich formatting, colors, and spinners;
- prints one structured JSON result to stdout for supported commands;
- captures Python hook stdout as `hook.stdout` events so it cannot corrupt the result;
- for `test`, `regression`, and `fpv`, renders the result summary as plain text on stderr and records it as a `summary` event carrying `rows` and `counts`.

Add `--print-failures-only` to drop `PASS`, `SKIP`, and `XFAIL` rows from that stderr render on a long run; the `summary` event still carries every row.

A hook that starts an external process inheriting file descriptor 1 can still write to stdout. Redirect that process explicitly; see [Hook execution context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/#handle-hook-execution-context).

### Know which commands write a log

Every command that runs a flow — `test`, `regression`, `synth`, `power` and the rest — attaches `<command_root>/rtl_buddy.log` and writes its events there. The file is opened for writing, and a process's first open of it truncates it: a flow's log is that run's log, not an accumulation of every run before it.

Read commands attach no file log. Those are `rb phys`, `rb cov`, the `rb graph` read verbs (`query`, `path`, `explain`), `rb xplr`, and `--list` on any flow command. They answer from artefacts and configs already on disk, so the log would be the only file they wrote — and writing it would truncate the log of the flow being asked about, which is the file to read next. Their events reach the console on stderr, and their result reaches stdout as JSON like any other structured command.

Read the log of the flow that produced the artefacts, not of the read verb that reported them.
