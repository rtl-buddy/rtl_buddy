## v5 to v6

Rename `budget.per_module_cap` to `budget.per_file_cap` in every `mut.yaml`. The old key is ignored, which removes the cap; it does not fail validation.
