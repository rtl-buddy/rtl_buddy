## Verilator randomized runs may not reproduce

Verilator can behave differently for the same random seed. When reproducibility matters, use VCS with `-xlrm hier_inst_seed` and give instances stable explicit names. If VCS does not write `HierInstanceSeed.txt` in the simulation directory, rtl_buddy warns `sim.hier_seed_missing` and cannot record the seed. The verdict is unchanged.
