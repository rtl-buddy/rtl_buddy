## Modify a test with `preproc`

Preprocessing runs after sweep expansion and before compile. Modify `test_cfg` directly:

```yaml
- name: basic
  preproc:
    path: my_preproc.py
  model: my_design
  model_path: ../src/models.yaml
  testbench: tb_top
```

```python
import os
from pathlib import Path

test_cfg.plusargs["BUILD_ID"] = os.environ.get("CI_BUILD_ID", "local")
test_cfg.plusargs["stimulus"] = str(
    Path(suite_dir) / "vectors" / "streaming_contract.txt"
)
```

The script receives `test_cfg`, `root_cfg`, `suite_dir`, `artifact_dir`, `logger`, and `__file__`, plus:

| Variable | Value |
|---|---|
| `run_id` | Run index for a dispatched element or single test; `None` when one hook invocation serves several local `randtest` runs |
| `run_artifact_dir` | `artifact_dir/run-NNNN` when `run_id` is set; otherwise `artifact_dir`. This is also the simulation working directory |

Both artefact directories exist before the hook runs. A script exception marks the affected test as a setup failure; remaining tests continue.
