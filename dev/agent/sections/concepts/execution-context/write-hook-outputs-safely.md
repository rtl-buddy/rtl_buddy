## Write hook outputs safely

In `sweep` and `preproc` scripts, use the supplied `suite_dir` and `artifact_dir` variables. The process working directory remains `invocation_cwd`.

```python
out = os.path.join(artifact_dir, "gen.sv")  # correct
out = os.path.join(os.getcwd(), "gen.sv")  # wrong: invocation cwd
```

The configured `postproc` script is not currently executed; built-in post-processing determines results. See [Hook execution context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/#handle-hook-execution-context).
