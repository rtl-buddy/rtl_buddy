## Troubleshoot builds

- **`dispatch.build_job_deduped` and the build job stays `PENDING`:** inspect the job it waits for with `squeue -j <ids> -O JobID,State,Reason` and `scontrol show job <id>`. `RUNNING`, or `PENDING` for `Resources` or `Priority`, is normal; cancelling it discards a build this run will reuse. `scancel` it only if it is held (`JobHeldUser`, `JobHeldAdmin`), unschedulable (`PartitionConfig`, `BadConstraints`), or an abandoned run.
- **`build_job.group_input_drift`:** configs that share one build have different inputs. Give the config its own build, or fix the hook that rewrites the input per test.
- **`compile.build_stamp_rejected`, `compile.stamp_write_failed`, `compile.prebuilt_stamp_invalid`:** see [Recover when a gated job cannot use the build](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/#recover-when-a-gated-job-cannot-use-the-build).
- **`dispatch.binary_mismatch`:** runs of one build directory did not all use the same executable, because one run rebuilt it while others used it. Results are scored as they ran; re-run to confirm.
- **`compile.build_phase_fallback`** (`marker-missing`, `marker-stale`, `no-verilate-unsupported`): the build job verilated for itself. The result is correct but unsplit.
- **`compile.verilate_failed`:** verilation failed; the build job reports it per test.
- **`compile.verilate_marker_write_failed`:** the verilate output could not be recorded, so the build job compiles that build in full.
- **`compile.license_queued`:** a VCS build waited for a license. Add `compile.time` headroom or lower `parallel`.
- **`build_job.compile_worker_error`:** the build job's compile raised an error. The test counts as a compile failure and its simulation job retries the compile.
