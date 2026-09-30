## Start with a model or testbench

Generate and serve a schematic from `models.yaml` at startup:

```bash
rb hub start --serve-viewer --model ip_demo_tiny_npu
rb hub start --serve-viewer --model ip_demo_tiny_npu \
  --models-file design/npu/models.yaml
```

`--model` requires `--serve-viewer`. Without `--models-file`, the hub searches the project and requires exactly one matching model; zero or several matches fail and list the candidates. Pass `--models-file` when names overlap.

The browser can switch designs without a restart:

- `GET /models` lists models and their view status.
- `GET /view.json?model=NAME` builds or reuses the model's view and activates it.
- `GET /tests` lists runnable testbench views.
- `GET /view.json?test=NAME` builds and activates a view rooted at the test's testbench.

Views are cached; restart the hub after changing RTL. A failed build never serves the previous build's hierarchy.
