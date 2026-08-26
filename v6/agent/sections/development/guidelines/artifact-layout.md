## Artifact Layout

Write generated outputs under `artefacts/<name>/`. Keep compile outputs (`run.f`, `compile.log`, builder output) in the test root and randomized simulation output in `run-NNNN/`. Latest-run symlinks are conveniences, not durable storage.

Every run writes `result.json` beside its durable output. Consumers use this envelope, not log parsing, for verdicts. Envelope writes are best-effort and must not turn a passing run into a failure. Dispatch also collects copies under `<test>/dispatch/result-<tag>.json`.
