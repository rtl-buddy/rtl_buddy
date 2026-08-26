## Start with a model or testbench

Generate and serve a schematic from `models.yaml` at startup:

```bash
rb hub start --serve-viewer --model ip_demo_tiny_npu
rb hub start --serve-viewer --model ip_demo_tiny_npu \
  --models-file design/npu/models.yaml
```

`--model` requires `--serve-viewer`. Without `--models-file`, the hub searches the project and requires exactly one matching model. Zero or multiple matches fail with the discovered files and model names. Use `--models-file` to constrain discovery when names overlap.

The browser can switch without restarting:

- `GET /models` lists models and their current view status.
- `GET /view.json?model=NAME` builds or reuses `.rtl-buddy/cache/view-<NAME>.json`, activates it, and broadcasts `view_changed`.
- `GET /tests` lists runnable testbench views.
- `GET /view.json?test=NAME` builds and activates a TB-rooted view from the test's model and testbench.

Model discovery is refreshed per request. View generation is serialized per model or test to prevent duplicate concurrent builds. Restart the hub when you need to force regeneration after source changes.
