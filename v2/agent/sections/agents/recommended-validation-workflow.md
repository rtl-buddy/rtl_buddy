## Recommended validation workflow

```bash
# 1. Check rtl_buddy version
rtl-buddy --version

# 2. Dry-run: verify pre-flight config without compiling or simulating
rtl-buddy --machine test basic --early-stop pre

# 3. Run a single test
rtl-buddy --machine test basic

# 4. Check the log for outcome
grep '"event"' rtl_buddy.log | tail -5

# 5. Run a full regression
rtl-buddy --machine regression -c design/regression.yaml
```

Use `--early-stop pre` to validate that config files, model paths, and testbench paths all resolve correctly before committing to a compile step.
