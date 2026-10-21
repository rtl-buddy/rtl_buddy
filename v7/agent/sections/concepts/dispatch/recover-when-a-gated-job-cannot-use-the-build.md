## Recover when a gated job cannot use the build

When a simulation job finds the build unusable, the outcome depends on what the build job recorded for that test. The job keeps its simulation reservation.

- **The build failed on the same inputs.** The job does not recompile. Its row reports the build job's exit status and error lines.
- **The build succeeded but is missing or does not match.** The job does not recompile. The row and `compile.build_stamp_rejected` say what was found. A mismatch usually means `preproc` generates different bytes on this node, or an edit landed mid-run.
- **The build succeeded but could not be recorded.** The build job logged `compile.stamp_write_failed`, usually from a read-only or full filesystem. Fix it and re-run.
- **Nothing proves the build cannot work.** This covers a crashed or cancelled build job, a `preproc` or filelist error, and inputs changed since the failure. The job recompiles at its simulation size, writes `compile.retry.log` in its run directory, and logs `compile.prebuilt_stamp_invalid`.

`cfg-dispatch.compile` does not size that recompile. Fix whatever changes the inputs, or size the simulation reservation for compilation.
