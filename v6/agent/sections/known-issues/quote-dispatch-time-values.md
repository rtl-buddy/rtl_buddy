## Quote dispatch time values

YAML 1.1 parses an unquoted value such as `time: 4:00:00` as an integer. rtl_buddy rejects it rather than submit a 10-day Slurm reservation. Use `time: "4:00:00"` or a quoted minute count everywhere `resources:` appears.
