## Exit codes

- `mut run` exits 0 when any result is scorable and 1 when the score is `n/a`. It does not gate on a score threshold.
- `mut list` and `mut score` exit 0 on success.
- Configuration, engine and report errors are fatal.
