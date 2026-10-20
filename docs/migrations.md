---
description: Required project changes for rtl_buddy major-version upgrades and the legacy submodule-to-uv migration.
---

# Migrations

Apply each section between your installed and target major versions, in order. The [submodule-to-uv migration](#submodule-to-uv) does not depend on the package version.

## v2 to v3

Per-test outputs moved from `logs/` to `artefacts/` inside the suite directory.

| v2 | v3 |
|----|-----|
| `logs/{test}.log` | `artefacts/{test}/test.log` |
| `logs/{test}.err` | `artefacts/{test}/test.err` |
| `logs/{test}.randseed` | `artefacts/{test}/test.randseed` |
| `logs/{test}.coverage.dat` | `artefacts/{test}/coverage.dat` |
| `logs/{test}.compile.log` | `artefacts/{test}/compile.log` |

`randtest` iterations write to numbered subdirectories such as `artefacts/{test}/run-0001/test.log`. Shared compile outputs (`compile.log`, `run.f`) stay in `artefacts/{test}/`. The suite-root `test.log`, `test.err`, and `test.randseed` are symlinks to the latest run.

Change `logs/` to `artefacts/` in `.gitignore`, CI, and coverage scripts. Coverage is `artefacts/{test}/coverage.dat` for one run and `artefacts/{test}/run-*/coverage.dat` for randomized runs. Build hook paths from `suite_dir`; v5 changed the hook working directory again.

## v3 to v4

`cfg-synth-libs` is replaced by reusable PDK data plus flow-specific platform selectors:

```yaml
# v3
cfg-synth-libs:
  - name: nangate45_typ
    path: pdk/.../typical.lib
    lef-paths: [...]

# v4
cfg-pdks:
  - name: nangate45
    corners: { typ: pdk/.../typical.lib }
    tech-lef: pdk/.../tech.lef
    macro-lef: pdk/.../cells.lef
cfg-synth-platforms:
  - { name: nangate45_typ, pdk: nangate45, corner: typ }
```

- In `synth.yaml`, replace `libraries: [name]` with `platform: name`.
- Add `cfg-pnr-platforms` only if you run `rb pnr`.
- API users: replace `get_synth_lib_cfg` with `get_synth_platform_cfg`, and use `get_pdk_cfg` and `get_pnr_platform_cfg` for the new layers.

See [Synthesis](concepts/synthesis.md) and [Place-and-Route](concepts/pnr.md).

## v4 to v5

Managed output is anchored on the primary config's directory, the [`command_root`](concepts/execution-context.md).

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own log |
| `root_config.yaml` discovery | from invocation cwd | from command root |
| `hier` / `axi-profile` default outputs | invocation cwd | command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

Explicit CLI paths still resolve from the invocation directory. Only managed artifacts and default output locations moved. Point CI at the command root for `rtl_buddy.log`.

### Hook scripts run at the invocation directory

`sweep` and `preproc` hooks run from the invocation directory. Build paths from the injected `suite_dir` and `artifact_dir`, never `os.getcwd()`:

```python
out  = os.path.join(artifact_dir, "gen.sv")
stim = os.path.join(suite_dir, "vectors", "in.txt")
```

If a third-party generator writes only relative to the working directory, wrap the call in a temporary `os.chdir(suite_dir)`.

## v5 to v6

Rename `budget.per_module_cap` to `budget.per_file_cap` in every `mut.yaml`. The old key is ignored, which removes the cap; it does not fail validation.

## Submodule to uv

Replace the `tools/rtl_buddy` submodule and editable pip install with a `uv` dependency:

```bash
uv init --bare        # only if there is no pyproject.toml yet
uv add rtl_buddy
uv run rb --version
```

1. Remove the `tools/rtl_buddy` submodule.
2. Move any `requirements.txt` entries into `dependencies` in `pyproject.toml`, then delete `requirements.txt`.
3. Change local scripts and CI from `tools/rtl_buddy/…` or `python -m rtl_buddy` to `uv run rb …`.
4. Commit `pyproject.toml` and `uv.lock` so other users and CI resolve the same environment.

For an exact pin, use `uv add "rtl_buddy==<version>"`.
