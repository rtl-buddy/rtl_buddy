## Read power results

Vivado runs `report_power` after routing and reports total, dynamic and static watts. These are vectorless estimates, suitable for comparing runs but not for signoff. Check the confidence and activity assumptions in `power.rpt` before treating them as absolute. openXC7 reports no power.
