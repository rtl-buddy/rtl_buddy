## Read event logs

Each line of a machine-mode `rtl_buddy.log` is one JSON event:

```json
{"event":"sim.completed","test":"smoke","duration_sec":4.2,"message":"smoke: simulation completed in 4.20s"}
{"event":"postproc.completed","test":"smoke","result":"PASS","desc":"smoke completed","message":"smoke: post-processing completed with result PASS"}
```

Switch on `event` and read its fields; do not parse `message`. For a test, `postproc.completed.result` and `.desc` are the verdict. A multi-suite run also writes a log in each suite directory.
