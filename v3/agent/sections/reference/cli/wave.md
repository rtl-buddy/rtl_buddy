## wave

```text
Usage: rtl-buddy wave [OPTIONS] TEST_NAME                                              
                                                                                        
 open waveform viewer for a test                                                        
                                                                                        
╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    test_name      TEXT  name of test to open waveform for [required]               │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --test-config     -c      TEXT  tests.yaml to use [default: tests.yaml]              │
│ --surfer                  TEXT  cfg-surfer entry name [default: surfer-default]      │
│ --resim                         force re-run of debug sim even if FST exists         │
│ --focused-signal                annotate only the signal selected via Go to          │
│                                 declaration; default annotates all signals in scope  │
│ --help                          Show this message and exit.                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
