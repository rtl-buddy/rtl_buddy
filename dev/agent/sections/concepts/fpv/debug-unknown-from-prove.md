## Debug `UNKNOWN` from `prove`

`bmc` checks behavior only to `depth`. `prove` uses k-induction, whose step may start from unreachable states. An `UNKNOWN` trace is either a real bug or a counterexample to induction.

1. Open the induction trace and decide whether its initial state is reachable.
2. Add invariants that exclude impossible predecessor states or relate pipeline stages.
3. Check environment assumptions: under-constrained ones create false failures, over-constrained ones hide bugs.
4. Raise `depth` only when the design needs more steps to become inductive. Depth-dependent proofs can break under design changes.

All assertions strengthen the induction hypothesis together, so a companion invariant can close another property.
