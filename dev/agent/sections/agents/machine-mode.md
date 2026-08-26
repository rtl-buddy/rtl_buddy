## Machine mode

Pass `--machine` before the subcommand:

```bash
rb --machine test basic
rb --machine regression -c regression.yaml
```

Machine mode:

- writes `rtl_buddy.log` as JSON Lines;
- disables Rich formatting, colors, and spinners;
- prints one structured JSON result to stdout for supported commands;
- captures Python hook stdout as `hook.stdout` events so it cannot corrupt the result.

A hook that starts an external process inheriting file descriptor 1 can still write to stdout. Redirect that process explicitly; see [Hook execution context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/#handle-hook-execution-context).
