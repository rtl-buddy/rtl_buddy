## Directory-level coverage summary

Add per-directory coverage breakdowns to the summary output using `--coverage-dir-summary`. Pass repo-relative directory prefixes; the flag may be repeated.

```bash
rtl-buddy --builder-mode cov regression \
  --coverage-merge \
  --coverage-dir-summary src/core \
  --coverage-dir-summary src/mem
```

Or provide prefixes from a file (one per line):

```bash
rtl-buddy --builder-mode cov regression \
  --coverage-merge \
  --coverage-dir-summary-file coverage_dirs.txt
```
