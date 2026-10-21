## rb power times every corner in one SDC mode

On a multi-corner platform `rb power` reads one SDC, by default the P&R run's `<top>.routed.sdc` (the primary corner's), and the routed SPEF once for all corners. Every corner then uses the primary corner's generated-clock latencies, and the corners after the first reuse the first corner's reduced parasitics. For one corner's own figures, run `rb power` on a single-corner platform of that corner with `constraints:` set to `<top>.<corner>.routed.sdc`.
