## Basic usage

```bash
cd verif/sandbox
uv run rb wave basic          # runs debug sim if no FST exists, then opens Surfer
uv run rb wave basic --resim  # force re-run of debug sim
```

Signal layout files are loaded automatically: if `basic.surfer` exists next to `tests.yaml`, Surfer opens with those signals pre-loaded.
