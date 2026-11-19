## Artifact Layout

Write generated outputs under `artefacts/<name>/`, or `artefacts/.runs/<tag>/<name>/` under `--run-tag`. Keep compile outputs (`run.f`, `compile.log`, builder output) in the test root and randomized simulation output in `run-NNNN/`. Latest-run symlinks are conveniences, not durable storage.

A reserved directory under `artefacts/` is dot-prefixed (`.shared-builds`, `.dispatch`, `.runs`) so it can never collide with a run named after it and so every scan that classifies directories by name keeps skipping it.

Every run writes `result.json` beside its durable output. A tagged run's envelope also carries `run_tag`; an untagged run's does not, so its bytes are unchanged. Consumers use this envelope, not log parsing, for verdicts. Envelope writes are best-effort and must not turn a passing run into a failure. A dispatched simulation job also writes its envelope to `<test>/dispatch/result-<tag>.json`, which the head collects. A run with no simulation job (in-process, or `-E comp` under dispatch) writes only `result.json`, so a consumer that reads both takes the newer as the run's record, as `rb graph results` does.
