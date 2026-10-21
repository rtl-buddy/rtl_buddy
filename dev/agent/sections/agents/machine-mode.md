## Machine mode

Pass `--machine` before the subcommand:

```bash
rb --machine test basic
rb --machine regression -c regression.yaml
```

Supported commands print one JSON result to stdout and render plain stderr. File logs use JSON Lines. Python hook stdout becomes `hook.stdout` events; redirect child-process stdout to protect the result. See [Hook execution context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/#handle-hook-execution-context).

Stderr summaries omit `PASS`, `SKIP`, and `XFAIL` rows but tally every verdict. Stdout results and the log's `summary` event retain all rows and counts. Human mode shows all rows unless `--print-failures-only` is set.

Per-phase status lines require `--verbose` or `--debug`. Dispatch progress, job announcements, hooks, warnings, and errors remain visible.
