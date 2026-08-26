## Hook scripts (`sweep`, `preproc`)

The `sweep` and `preproc` hook scripts execute via `exec()` inside the `rb` process and receive `suite_dir` and `artifact_dir` as namespace variables. **Always use these variables.** Do not call `os.getcwd()` inside a hook — the process CWD stays at `invocation_cwd` (the same as your shell), which is no longer the same as `suite_dir`. (The `postproc` hook is parsed from config but the runtime currently relies on built-in post-processing rather than running a user script — see [Plugins](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/plugins/).)

```python
# inside a sweep / preproc script
import os
out = os.path.join(artifact_dir, "gen.sv")   # correct
out = os.path.join(os.getcwd(), "gen.sv")    # wrong — invocation cwd
```
