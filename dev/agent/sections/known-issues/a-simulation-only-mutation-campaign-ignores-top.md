## A simulation-only mutation campaign ignores `top`

Only the FPV oracle elaborates a top; the simulation oracle runs the test suite's own testbenches. A `mut.yaml` that declares `top:` but configures no `verify.fpv_config` therefore logs `mut_config.top_override_unused` at load and scores every mutant unchanged. Remove the field, or add the FPV oracle the top is meant to root. A campaign with an FPV oracle applies its `top` to the baseline proof and to every mutant proof. See [Mutation Testing](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/mut/).
