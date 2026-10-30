## Write generated files safely

Pick the output directory by how the data varies:

- Test-invariant output goes in `artifact_dir`. Concurrent dispatched runs share it, so write to a temporary file and publish it with `os.replace()`.
- Run- or seed-specific output goes in `run_artifact_dir`. It is unique only when `run_id` is set. Local `randtest` calls `preproc` once for all seeds, so use dispatch or `sweep` when generated data must differ per seed.

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

Resolve suite inputs from `suite_dir`, never `os.getcwd()`. Plusargs pass through verbatim, so give suite-local input paths explicitly. A relative output path lands in `run_artifact_dir` because simulation runs there.
