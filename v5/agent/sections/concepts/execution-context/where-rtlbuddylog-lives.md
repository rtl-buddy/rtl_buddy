## Where `rtl_buddy.log` lives

The orchestration log is always written to `command_root/rtl_buddy.log`. In `--machine` mode it is JSONL; otherwise plain text. For `regression`, each suite's iteration re-anchors the log to that suite's directory, and the final summary phase re-anchors back to `dirname(regression.yaml)`. Open the latest log from wherever the *primary* config lives, not from where you ran `rb`.
