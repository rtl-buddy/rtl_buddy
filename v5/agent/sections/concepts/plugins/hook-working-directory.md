## Hook working directory

`sweep` and `preproc` hooks execute via `exec()` inside the `rb` process and share its working directory, which stays at `invocation_cwd` — the directory you ran `rb` from. It is **not** the suite directory. Always build paths from the injected `suite_dir` and `artifact_dir` variables; never call `os.getcwd()` to locate the suite.

```python
import os
out = os.path.join(artifact_dir, "gen.sv")   # correct
out = os.path.join(os.getcwd(), "gen.sv")     # wrong — invocation cwd
```

If a hook delegates to a third-party generator that writes relative to `os.getcwd()` and exposes no output-directory argument, wrap the call in a `chdir` to the suite and restore it afterwards:

```python
prev = os.getcwd()
os.chdir(suite_dir)
try:
    gen_dir = third_party_generate(...)   # writes relative to cwd
finally:
    os.chdir(prev)
```

This anchoring behavior changed in v5; see [Migrating from v4 to v5](https://rtl-buddy.github.io/rtl_buddy/v5/migrations/v4-to-v5/) and the [execution context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/) reference.
