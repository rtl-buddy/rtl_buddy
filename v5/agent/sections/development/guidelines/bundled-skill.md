## Bundled Skill

The `rtl_buddy` agent skill ships inside the wheel at `src/rtl_buddy/skill/` and is installed by `rtl-buddy skill install`.
There is no separate source-of-truth skill repo.

Keep `src/rtl_buddy/skill/SKILL.md` short and agent-specific.
Prefer links to local docs commands over duplicating reference content.
Project-level installs are an override mechanism; the default install scope should remain user-level unless the policy is deliberately revisited.
