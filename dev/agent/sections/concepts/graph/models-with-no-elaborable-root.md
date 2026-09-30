## Models with no elaborable root

The design tier elaborates each model from its `top:`, which defaults to the model name. If no module has that name (an SV `interface` published as a library entry, a filelist of vendored IP), set `top:` to the real root module or set `graph: false` in [`models.yaml`](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#modelsyaml).

- A `graph: false` model is listed under the design tier's `skipped` entries, not `failures`, and does not change the exit code even with `--strict`. If every model in scope opts out, the whole tier is `skipped`.
- Its cocotb testbenches and synth, CDC and FPGA runs that would elaborate the same root are skipped with it, and a hierarchy exported earlier is deleted.
- The model keeps its config-tier node, so `spec:` and test references resolve, but it has no edge into the design tier. A model outside the `--model` or `-c` selection is treated the same way.
- `rb hier`, `rb hier-query` and `rb axi-profile` ignore `graph: false`.
