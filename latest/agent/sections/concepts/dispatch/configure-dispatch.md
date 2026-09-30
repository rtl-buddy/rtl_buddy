## Configure dispatch

Set defaults in `root_config.yaml`:

```yaml
cfg-dispatch:
  backend: slurm
  jobs: 4
  resources:
    cpus: 2
    mem: 4G
    time: "01:00:00"
  compile:
    cpus: 8
    mem: 16G
    time: "02:00:00"
    parallel: 4          # builds compiled at once in the build job
    split-verilate: true # Verilator suites verilate in their own Slurm job
  sbatch-args:
    - --partition=verif
    - --account=chip
  max-jobs-per-array: 200
  max-array-size: 1001   # omit to read MaxArraySize from `scontrol show config`
  progress-interval: 60
  max-wait: 7200
  retry:
    attempts: 2
    backoff-sec: 60
  rightsize:
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

`jobs` sizes the local-parallel pool. `max-jobs-per-array` throttles each Slurm array. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml) for defaults and validation.

Quote every `time` value; an unquoted `4:00:00` is rejected.
