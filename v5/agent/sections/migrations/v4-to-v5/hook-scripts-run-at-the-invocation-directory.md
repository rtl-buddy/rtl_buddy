## Hook scripts run at the invocation directory

This is the one change most likely to break existing projects.

`sweep` and `preproc` hooks execute via `exec()` inside the `rb` process and share its working directory. **The change is specific to `regression`:** in v4 it did `os.chdir()` into each suite, so hooks ran from the suite directory; v5 removes that chdir, so hooks now run at `invocation_cwd` — your shell's directory — like every other command.

Single-suite `test` and `randtest` are unaffected here: their hook working directory was already `invocation_cwd` in v4, so nothing changes for them. (Their *artefact* locations do move under the suite in v5 — see the table above — but that is anchored independently of the hook's cwd.)

In all cases, hooks receive `suite_dir` and `artifact_dir` as namespace variables. Build paths from those:

```python
# inside a sweep / preproc script
import os
out = os.path.join(artifact_dir, "gen.sv")          # correct
stim = os.path.join(suite_dir, "vectors", "in.txt")  # correct
out = os.path.join(os.getcwd(), "gen.sv")            # wrong — invocation cwd
```

Any hook that called `os.getcwd()` (directly or indirectly) to find the suite will break.

### Third-party generators that write relative to cwd

If a hook delegates to a generator you don't control — one that writes its outputs relative to `os.getcwd()` and exposes no output-directory parameter — `suite_dir`/`artifact_dir` can't help, because you can't tell the generator where to write. Wrap the call in a `chdir` to the suite (restore afterwards):

```python
prev = os.getcwd()
os.chdir(suite_dir)
try:
    gen_dir = third_party_generate(...)   # writes relative to cwd
finally:
    os.chdir(prev)
```

See [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/v5/known-issues/) for the failure signature when this is missed.
