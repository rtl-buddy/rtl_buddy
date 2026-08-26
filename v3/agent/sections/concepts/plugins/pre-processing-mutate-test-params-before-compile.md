## Pre-processing: mutate test params before compile

The pre-processing hook runs after sweep expansion but before the compilation step. Use it to dynamically adjust plusargs, plusdefines, or other test parameters based on runtime state.

**`tests.yaml` entry:**

```yaml
- name: "basic"
  preproc:
    path: "my_preproc.py"
  model: "my_design"
  model_path: "../src/models.yaml"
  testbench: "tb_top"
```

**Available variables:**

| Variable | Type | Description |
|----------|------|-------------|
| `logger` | Logger | Use for all logging |
| `test_cfg` | TestConfig (mutable) | Modify this to change compile/sim parameters |
| `root_cfg` | RootConfig (mutable) | The loaded root config |
| `suite_dir` | string | Absolute path to the directory containing `tests.yaml` |
| `artifact_dir` | string | Artifact root for this test under `suite_dir/artefacts/` |
| `__file__` | string | Absolute path to the current pre-processing script |

Plusargs are still passed through verbatim. If a plusarg value should reference a suite-local file, resolve it explicitly against `suite_dir` in preproc. Output filenames that should land in the per-test artefact tree can remain relative to `artifact_dir`.

**Example:**

```python
# my_preproc.py
import os
from pathlib import Path

test_cfg.plusargs["BUILD_ID"] = os.environ.get("CI_BUILD_ID", "local")
test_cfg.plusargs["stimulus"] = str(Path(suite_dir) / "vectors" / "streaming_contract.txt")
```

If a pre-processing script raises an exception, the affected test is marked as a setup failure and the rest of the run continues.

See the template repo for a working example.
