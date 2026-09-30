## `rb phys module` reports no power for an RTL module

The physical model's synthesis half holds RTL module names and its power half holds the Liberty cell of each leaf instance, so `rb phys module u_cpu` reports cell count and area with no instances and no power. Use `rb phys instance u_cpu`, which sums the leaf rows under the instance path. See [Physical Metrics](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/phys/#what-the-module-join-can-answer).
