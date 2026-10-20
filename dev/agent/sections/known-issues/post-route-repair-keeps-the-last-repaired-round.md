## Post-route repair keeps the last repaired round

The `post_route_repair` stage keeps the last round that repaired, even when that round's extracted timing is worse than an earlier round's or its detailed route has DRC violations, and `finish` continues from it. Compare the rounds' `pnr.post_route_round` events (also in `progress.jsonl`) before relying on more than one round, and lower `rounds` or `margin-step` when a later round loses slack. See [rtl_buddy#884](https://github.com/rtl-buddy/rtl_buddy/issues/884).
