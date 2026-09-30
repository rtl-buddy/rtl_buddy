## Design compile errors under dispatch are CompileFail

A design compile error is `CompileFail`; infrastructure failures stay `DispatchFail`. Simulation jobs do not recompile a config the build job recorded as failed, and the row carries the build job's error and logs. Fix the design or the build reservation, not the simulation one.
