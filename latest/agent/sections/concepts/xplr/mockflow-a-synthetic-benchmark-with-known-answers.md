## Mockflow: a synthetic benchmark with known answers

Use `rb xplr mock` to test an exploration policy without EDA runtime:

```bash
rb --machine xplr mock info --scenario zdt1
rb --machine xplr mock run --scenario zdt1 --register
rb --machine xplr mock score --scenario zdt1
```

`mock info` returns knob domains, costs, infeasible combinations, and analytic ground truth. `mock run` deterministically evaluates a knob vector; `--noise` adds seeded objective noise and `--register` records the experiment and outcome together. Without `--register`, the payload's `outcome` can be passed directly to `attach-outcome`.

Available scenarios are `rastrigin` for single-objective WNS maximization and `zdt1` for LUT/delay minimization. `mock score` reports regret for a single objective or hypervolume and distance-to-front for multiple objectives.

When registering mock results outside a Git repository, provide `--source-sha` and optionally `--source-branch`.
