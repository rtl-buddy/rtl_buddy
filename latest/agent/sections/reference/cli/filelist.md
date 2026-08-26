## filelist

```text
Usage: rtl-buddy filelist [OPTIONS] MODEL_NAME [OUTPUT_PATH]

 generate filelists using models.yaml

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    model_name       TEXT           name of model [required]                        │
│      output_path      [OUTPUT_PATH]  Output filename [default: run.f]                │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model-config  -c      TEXT  model_config.yaml to use [default: models.yaml]        │
│ --unroll        -u            Recursively unroll -F in filelists                     │
│ --flatten       -f            Remove path to a file, leaving just the filename       │
│ --strip         -s            Remove option part of a line                           │
│ --deduplicate   -d            Remove duplicates                                      │
│ --help                        Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
