## A simulation-only mutation campaign ignores `top`

Only the FPV oracle elaborates a top; the simulation oracle runs the suite's own testbenches. A `mut.yaml` that declares `top:` without `verify.fpv_config` warns `mut_config.top_override_unused` and scores every mutant unchanged. Remove the field, or add the FPV oracle it is meant to root. See [Mutation Testing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/mut/).
