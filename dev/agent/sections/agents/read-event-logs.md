## Read event logs

Each line in a machine-mode `rtl_buddy.log` is one JSON event:

```json
{"event":"sim.completed","test":"smoke","duration_sec":4.2,"message":"smoke: simulation completed in 4.20s"}
{"event":"postproc.completed","test":"smoke","result":"PASS","desc":"smoke completed","message":"smoke: post-processing completed with result PASS"}
```

Use `event` as the discriminator and consume event-specific fields rather than parsing `message`. For a test, `postproc.completed.result` and `.desc` are authoritative. Multi-suite runs also write a log in each suite directory.
