## Inspect results

Each suite writes orchestration output to `rtl_buddy.log` and per-test output under `artefacts/<test>/`. A `randtest` iteration uses `artefacts/<test>/run-NNNN/`; latest-run symlinks remain at the test artefact root.

For programmatic output:

```bash
uv run rb --machine test basic
```

See [Agent Use](https://rtl-buddy.github.io/rtl_buddy/v6/agents/#machine-mode) for the JSON contract and [Tests](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#interpret-results) for verdicts and exit codes.
