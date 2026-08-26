## Supported backend

Today only `rtl-buddy-cdc` is wired up. The `tool:` field in `cdc.yaml` selects it; the runner raises a clear error if no matching `cfg-cdc-tools` entry exists. Adding a commercial backend parallels how `rb fpv` is structured — implement a sibling driver under `src/rtl_buddy/tools/`, then dispatch from `CdcRunner`.
