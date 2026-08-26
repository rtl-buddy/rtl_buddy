## verible filelist

```text
Usage: rtl-buddy verible filelist [OPTIONS]                                            
                                                                                        
 generate verible.filelist from models.yaml so verible-verilog-ls can resolve           
 cross-file symbols                                                                     
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --model           TEXT  Model name(s) to include. May be repeated. Default: union of │
│                         every model declared in any models.yaml under the project    │
│                         root.                                                        │
│ --output  -o      TEXT  Output path. Defaults to <project_root>/verible.filelist so  │
│                         verible-verilog-ls auto-discovers it.                        │
│ --help                  Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
