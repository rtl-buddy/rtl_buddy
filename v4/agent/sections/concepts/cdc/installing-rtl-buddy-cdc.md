## Installing rtl-buddy-cdc

```bash
uv tool install rtl-buddy-cdc    # recommended — isolated tool env
# or
uv pip install rtl-buddy-cdc     # into the project venv
```

Once installed, the binary lands on `PATH` as `rtl-buddy-cdc`. The default `cfg-cdc-tools` entry in `root_config.yaml` resolves it from `PATH`; override with an absolute path if you need a specific version.
