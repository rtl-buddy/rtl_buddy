## hub send overlay

```text
Usage: rtl-buddy hub send overlay [OPTIONS] NAME                                       
                                                                                        
 Flip an overlay's enabled state on the SPA. Built-in NAMES are 'clock', 'reset',       
 'axi-perf', 'wave'; an unknown name is a no-op. Use --on / --off (default --on).       
 Useful for agents or scripted demos that want to direct the user's attention to a      
 specific overlay layer without a UI click.                                             
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    name      TEXT  overlay name [required]                                         │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --on      --off      Enable (default) or disable the named overlay. [default: on]    │
│ --help               Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
