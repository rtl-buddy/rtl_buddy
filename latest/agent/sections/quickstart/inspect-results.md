## Inspect results

Each suite writes orchestration output to `rtl_buddy.log` and per-test output to `artefacts/<test>/`. A `randtest` iteration writes to `artefacts/<test>/run-NNNN/`, and latest-run symlinks stay at the test artefact root.

For JSON output:

```bash
uv run rb --machine test basic
```

See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/v6/agents/#machine-mode) for the JSON contract and [Tests](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#interpret-results) for verdicts and exit codes.
