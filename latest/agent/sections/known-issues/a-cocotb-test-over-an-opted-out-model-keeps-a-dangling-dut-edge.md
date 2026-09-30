## A cocotb test over an opted-out model keeps a dangling DUT edge

`graph: false` in `models.yaml` withdraws every config-tier edge into the model's hierarchy, but the cocotb edge to `module:<toplevel>` still names the DUT and appears in the `merge.dangling` list of `graph-meta.json`. Opt out only models that no cocotb test runs against, or give the model a `top:`.
