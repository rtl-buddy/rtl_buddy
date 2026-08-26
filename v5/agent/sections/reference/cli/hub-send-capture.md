## hub send capture

```text
Usage: rtl-buddy hub send capture [OPTIONS]                                            
                                                                                        
 Ask the view peer (SPA) to snapshot the current graph and write it to --out.           
 Graph-only — surrounding panels are not captured. Useful for agents that want to look  
 at what the user is seeing without a browser screenshot tool.                          
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ *  --out      -o      PATH                         Destination file. Extension       │
│                                                    determines format if --format     │
│                                                    omitted.                          │
│                                                    [required]                        │
│    --format   -f      TEXT                         png (default) or svg. Inferred    │
│                                                    from --out suffix if not given.   │
│    --scale            FLOAT RANGE [0.1<=x<=8.0]    PNG upscale factor (1.0 =         │
│                                                    native). Ignored for SVG.         │
│                                                    [default: 1.0]                    │
│    --timeout          FLOAT RANGE [1.0<=x<=120.0]  Seconds to wait for the SPA to    │
│                                                    reply. Large designs may need     │
│                                                    longer.                           │
│                                                    [default: 15.0]                   │
│    --help                                          Show this message and exit.       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
