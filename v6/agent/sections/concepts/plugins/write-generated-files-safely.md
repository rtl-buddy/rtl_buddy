## Write generated files safely

Choose the output directory from the data's lifetime:

- Write test-invariant output to `artifact_dir`. Concurrent dispatched runs share this directory, so publish files atomically with a temporary file and `os.replace()`.
- Write run- or seed-specific output to `run_artifact_dir`. It is unique only when `run_id` is set. Local `randtest` invokes preproc once for all seeds, so use dispatch or sweep when generation must vary per seed.

```python
import os
from pathlib import Path

if run_id is not None:
    (Path(run_artifact_dir) / "stimulus.hex").write_text(generate(run_id))
else:
    out = Path(artifact_dir) / "stimulus.hex"
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    tmp.write_text(generate())
    os.replace(tmp, out)
```

Resolve suite inputs from `suite_dir`; do not use `os.getcwd()`. Plusargs are passed verbatim, so make suite-local input paths explicit. Relative output paths may target `run_artifact_dir` because simulation runs there.
