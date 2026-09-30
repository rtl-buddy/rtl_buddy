## Modify a test with `preproc`

Preprocessing runs after sweep expansion and before compile. Modify `test_cfg` in place:

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

A `preproc` script receives the sweep variables except `out_test_cfgs`, plus:

| Variable | Value |
|---|---|
| `run_id` | Run index for a dispatched element or a single test. `None` when one invocation serves several local `randtest` runs. |
| `run_artifact_dir` | `artifact_dir/run-NNNN` when `run_id` is set, otherwise `artifact_dir`. Also the simulation working directory. |

Both directories exist before the hook runs. If the script raises, the test is a setup failure and the remaining tests continue.
