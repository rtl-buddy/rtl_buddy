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
