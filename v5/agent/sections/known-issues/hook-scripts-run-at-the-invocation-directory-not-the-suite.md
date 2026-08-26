## Hook scripts run at the invocation directory, not the suite

`sweep` and `preproc` hooks execute via `exec()` inside the `rb` process and share its working directory, which is `invocation_cwd` — your shell's cwd — not the suite directory. Resolve suite-local inputs and outputs from the injected `suite_dir` / `artifact_dir` variables, never from `os.getcwd()`.

The footgun is a hook that delegates to a **third-party generator** which writes its outputs relative to `os.getcwd()` and offers no output-directory argument. Under v4, `regression` chdir'd into each suite so such a generator dropped files under the suite; under v5 it drops them under the invocation directory instead (e.g. polluting the repo root when running `regression` from there). The failure is silent: generation "succeeds", but the test fails much later at sim time with a generic `cannot open <suite_dir>/<gen_dir>/<file>`. It only reproduces when `invocation_cwd != suite_dir`, so it passes when run from inside the suite and fails under `regression` from the repo root.

Wrap the generator call in a `chdir` to the suite (restore afterwards):

```python
prev = os.getcwd()
os.chdir(suite_dir)
try:
    gen_dir = third_party_generate(...)   # writes relative to cwd
finally:
    os.chdir(prev)
```

See [Migrating from v4 to v5](https://rtl-buddy.github.io/rtl_buddy/v5/migrations/v4-to-v5/) for the full behavior change.
