## Troubleshoot early release

- **`dispatch.gates_unavailable` / `dispatch.release_failed`:** the build job could not release its simulations, or the scheduler refused. Those jobs start when the build job ends. A timeout or unusable `scontrol` turns release off for the rest of the build job.
- **`dispatch.release_skipped`:** a build's record could not be written, so its jobs are not released.
- **`dispatch.release_unavailable`:** `scontrol` is not on the compute node's PATH; nothing is released early.
- **`dispatch.build_result_partial` / `dispatch.build_result_final_write_lost`:** the build job did not finish, or its final record was lost. The head uses the records it has; tests without one count as missing results.
