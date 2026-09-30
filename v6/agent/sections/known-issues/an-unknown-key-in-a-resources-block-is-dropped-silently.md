## An unknown key in a `resources:` block is dropped silently

A `resources:` block discards any key it does not define, with no warning. A typo such as `memory:` reserves nothing while reading as if it did. Check a new reservation against [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#parallel-dispatch) and in the `Reserved` column of the reservation advice or the job's `--mem` and `--time`. A `modes:` block rejects unknown keys, but a release without `modes:` drops the whole block silently, so confirm a new mode's reservation once.
