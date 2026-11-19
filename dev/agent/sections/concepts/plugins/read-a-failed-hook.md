## Read a failed hook

A hook that raises fails its test with a description that names the exception and the innermost line of the hook script it passed through:

```text
Setup failed in preproc: KeyError: 'width' at my_preproc.py:12
Setup failed in sweep: AssertionError at example_sweep.py:7
```

The same text is the `error` field of the ERROR `preproc.failed` or `sweep.failed` event. The traceback, starting at the hook's first frame, is the INFO `hook.traceback` event: it is in `rtl_buddy.log` and, under dispatch, in the log of the job that ran the hook (see [Monitor and stop a run](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#monitor-and-stop-a-run)), but not on the console unless `-v` is given.
