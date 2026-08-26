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
  sbatch-args:
    - --partition=verif
    - --account=chip
  max-jobs-per-array: 200
  poll-interval: 10
  progress-interval: 60
  max-wait: 7200
  retry:
    attempts: 2
    backoff-sec: 60
    backoff-max-sec: 600
    jitter: 0.5
    classifiers: [license-queue]
  rightsize:
    report: true
    over-threshold: 0.5
    near-limit: 0.9
    margin: 1.5
```

`jobs` controls the single local-parallel pool. `max-jobs-per-array` controls each Slurm array. See [YAML formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#root_configyaml) for defaults and validation.

Always quote `time` values. YAML 1.1 can parse an unquoted value such as `4:00:00` as the integer `14400`, changing its meaning. rtl_buddy rejects that form. Quote times in global, compile, testbench, and test reservations.
