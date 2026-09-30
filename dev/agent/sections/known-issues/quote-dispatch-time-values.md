## Quote dispatch time values

YAML 1.1 reads an unquoted `time: 4:00:00` as an integer, and rtl_buddy rejects it. Quote every `time` value in `resources:`, `compile:` and `modes:` blocks.
