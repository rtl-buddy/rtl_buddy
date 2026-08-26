## Supported backend

Today only `sby` is wired up. The `tool:` field in `fpv.yaml` selects it; the runner raises a clear error if no matching `cfg-fpv-tools` entry exists. Adding a second backend (e.g. a commercial formal tool) parallels how [`rb cdc` is structured](https://github.com/rtl-buddy/rtl_buddy/issues/85) — implement a sibling driver under `src/rtl_buddy/tools/`, then dispatch from `FpvRunner`.
