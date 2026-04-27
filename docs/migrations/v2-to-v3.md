---
description: How to migrate an rtl_buddy project from v2 to v3, covering the artifact layout change and other breaking changes.
---

# Migrating from v2 to v3

## Artifact layout change

v3 changes where test outputs are written. All per-test files now live under `artefacts/` in the suite directory instead of `logs/`.

### Single run

| v2 | v3 |
|----|-----|
| `logs/{test_name}.log` | `artefacts/{test_name}/test.log` |
| `logs/{test_name}.err` | `artefacts/{test_name}/test.err` |
| `logs/{test_name}.randseed` | `artefacts/{test_name}/test.randseed` |
| `logs/{test_name}.coverage.dat` | `artefacts/{test_name}/coverage.dat` |
| `logs/{test_name}.compile.log` | `artefacts/{test_name}/compile.log` |

### Repeated runs (`randtest`)

Each `randtest` iteration now writes into a numbered subdirectory:

```
artefacts/{test_name}/run-0001/test.log
artefacts/{test_name}/run-0001/test.err
artefacts/{test_name}/run-0001/test.randseed
artefacts/{test_name}/run-0001/coverage.dat
```

Compile outputs (`compile.log`, `run.f`) remain at `artefacts/{test_name}/` — they are shared across all iterations of the same test.

### Symlinks

The convenience symlinks `test.log`, `test.err`, and `test.randseed` at the suite root still exist and still point to the latest run.

## What to update

### `.gitignore`

Replace any `logs/` entry with `artefacts/`:

```diff
-logs/
+artefacts/
```

### CI scripts

Update any scripts that reference `logs/{test_name}.*` paths to the new locations above.

### Coverage path references

Scripts that process coverage files should look for `artefacts/{test_name}/coverage.dat` (single run) or `artefacts/{test_name}/run-*/coverage.dat` (randtest).
