## Write hook outputs safely

`sweep` and `preproc` scripts run with the working directory at `invocation_cwd`. Build output paths from the supplied `suite_dir` and `artifact_dir` variables:

```python
out = os.path.join(artifact_dir, "gen.sv")  # correct
out = os.path.join(os.getcwd(), "gen.sv")  # wrong: invocation cwd
```

A configured `postproc` script is accepted but not executed; built-in post-processing decides results. See [Hook execution context](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/plugins/#handle-hook-execution-context).
