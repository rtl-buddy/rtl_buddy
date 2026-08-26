## Supported backend

Today only `openroad` is wired up. The `tool:` field in `power.yaml` selects it; the dispatch is a registry (`_POWER_BACKENDS` in `runner/power_runner.py`), so a commercial backend is one line plus a `BasePower` subclass under `tools/power_<name>.py`.
