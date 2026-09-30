## Machine mode

Pass `--machine` before the subcommand:

```bash
rb --machine test basic
rb --machine regression -c regression.yaml
```

In machine mode:

- Commands that write `rtl_buddy.log` write it as JSON Lines.
- Rich formatting, colors, and spinners are off.
- Supported commands print one structured JSON result to stdout.
- Python hook stdout is captured as `hook.stdout` events so it cannot corrupt the result.
- `test`, `regression`, and `fpv` render their result summary as plain text on stderr and record it as a `summary` event with `rows` and `counts`.

Add `--print-failures-only` to omit `PASS`, `SKIP`, and `XFAIL` rows from the stderr summary of a long run. The `summary` event still carries every row.

A hook that starts an external process inheriting file descriptor 1 can still write to stdout. Redirect that process; see [Hook execution context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/#handle-hook-execution-context).
