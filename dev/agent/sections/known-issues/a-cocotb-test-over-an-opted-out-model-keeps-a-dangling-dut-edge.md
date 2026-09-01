## A cocotb test over an opted-out model keeps a dangling DUT edge

`models.yaml` `graph: false` withdraws every config-tier edge into the model's hierarchy, but the binding tier's cocotb hop `python_module --binds_to--> module:<toplevel>` is derived from the merged graph and still names the DUT. That id stays in `graph-meta.json`'s `merge.dangling` list, exactly as it does under `--no-design`. Opt out only models that no cocotb test runs against, or give the model a `top:` instead.
