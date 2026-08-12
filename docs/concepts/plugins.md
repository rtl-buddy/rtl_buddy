---
description: How to extend rtl_buddy test behavior using sweep, preproc, and postproc Python plugin hooks.
---

# Plugins

`rtl_buddy` supports three Python plugin hooks that let you extend test behavior without modifying the tool itself. All hooks are specified per-test in `tests.yaml` and are executed by the tool at the appropriate point in the test flow.

Hook scripts receive their input through named variables injected into the script's namespace. They do not use `import` or function arguments — instead they read from and write to these predefined variables.

## Sweep: expanding one test into many

The sweep hook runs before the test flow and expands a single test entry into multiple `TestConfig` objects, each with different parameters. Use it to cover a combinatorial space of plusargs, seeds, or configurations without manually listing every variant.

**`tests.yaml` entry:**

```yaml
- name: "sweep_case"
  sweep:
    path: "example_sweep.py"
  model: "my_design"
  model_path: "../src/models.yaml"
  testbench: "tb_top"
  reglvl: 2000
```

**Available variables in the script:**

| Variable | Type | Description |
|----------|------|-------------|
| `logger` | Logger | Use this for all logging so output goes through `rtl_buddy`'s log system |
| `test_cfg` | TestConfig (immutable) | The original test entry from `tests.yaml` |
| `root_cfg` | RootConfig (mutable) | The loaded root config |
| `suite_dir` | string | Absolute path to the directory containing `tests.yaml` |
| `artifact_dir` | string | Artifact root for the incoming test name under `suite_dir/artefacts/` |
| `out_test_cfgs` | list | **Assign** the expanded list of `TestConfig` objects here |
| `__file__` | string | Absolute path to the current sweep script |

Everything in `TestConfig` except `reglvl` can be mutated in the generated tests (e.g. change `name`, `plusargs`, `plusdefines`).

**Example:**

```python
# example_sweep.py
out_test_cfgs = []
for i in range(4):
    cfg = test_cfg.copy()
    cfg.name = f"{test_cfg.name}_{i}"
    cfg.plusargs["SCENARIO"] = str(i)
    out_test_cfgs.append(cfg)
```

If the sweep script raises an exception, `rtl_buddy` records that test as a setup failure and continues with the remaining tests.

See the template repo for a working example.

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
| `artifact_dir` | string | Artifact root for this test under `suite_dir/artefacts/` — **test-keyed**, so every run of the test shares it |
| `run_id` | int or None | The run index this hook is preparing — a dispatched array element, or a single `test` — and `None` when one execution of the hook serves several runs, which is what a local `randtest` does (it runs `preproc` and the compile once, then loops the simulation) |
| `run_artifact_dir` | string | Artifact root for *this run*: `artifact_dir/run-NNNN` when `run_id` is set, otherwise `artifact_dir` itself. Also the simulation's working directory |
| `__file__` | string | Absolute path to the current pre-processing script |

Both directories exist by the time the hook runs.

Plusargs are still passed through verbatim. If a plusarg value should reference a suite-local file, resolve it explicitly against `suite_dir` in preproc. Output filenames that should land in the per-test artefact tree can remain relative to `artifact_dir`.

### Where a generator should write

`artifact_dir` is keyed on the test name only, so under `randtest` or `--dispatch` every seed of a test resolves to the same path — and dispatched seeds run **concurrently**. Which directory to use follows from what the generated files depend on:

- **Output depends only on the test** (the common case): write to `artifact_dir`, and write **atomically** — a temp file plus `os.replace`, never `open(path, "w")`. Truncate-in-place is not atomic, so a sibling element reading the file mid-write gets a short one, and the mismatch surfaces as a design failure rather than a harness failure.
- **Output depends on the run or the seed**: write to `run_artifact_dir`. It is unique per run, so nothing races, and it is the simulation's working directory — a plusarg naming a file there can stay relative. This only separates runs where the hook itself runs per run: under `--dispatch` it does, and `run_id` is set. A **local** `randtest` runs the hook once for all its seeds, so `run_id` is `None` and `run_artifact_dir` is the test directory — a generator that must vary per seed needs the dispatch path (or a `sweep` that expands the seeds into separate tests).

```python
# Seed-dependent stimulus: per-run directory, no race to worry about.
out = Path(run_artifact_dir) / "stimulus.hex"
out.write_text(generate(run_id))

# Test-dependent stimulus: shared path, so publish it atomically.
out = Path(artifact_dir) / "stimulus.hex"
tmp = out.with_suffix(f".{os.getpid()}.tmp")
tmp.write_text(generate())
os.replace(tmp, out)
```

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

## Hook working directory

`sweep` and `preproc` hooks execute via `exec()` inside the `rb` process and share its working directory, which stays at `invocation_cwd` — the directory you ran `rb` from. It is **not** the suite directory. Always build paths from the injected `suite_dir` and `artifact_dir` variables; never call `os.getcwd()` to locate the suite.

Because hooks run via `exec()` rather than `import`, `__name__` is set to the sentinel `"__rtl_buddy_hook__"` — never `"__main__"`. Put hook logic at module top level. If you also want the script runnable standalone (e.g. for local testing outside `rb`), keep only the standalone entry point under `if __name__ == "__main__":` and put the `rb`-invoked logic at module level or in the accompanying `else:` branch; the `__main__` branch is always skipped when `rb` runs the hook.

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

This anchoring behavior changed in v5; see [Migrations: v4 to v5](../migrations.md#v4-to-v5) and the [execution context](execution-context.md) reference.

## Post-processing

The `postproc` hook is parsed from config but the runtime flow currently relies on built-in post-processing. Custom post-processing support is planned for a future release.
