## Preserve Packaging And Installation Contracts

The primary source is `src/rtl_buddy/skill/SKILL.md`; specialists live at `src/rtl_buddy/skill/<skill-name>/SKILL.md`. Hatchling includes that tree in the wheel. Installed family members are sibling directories under the selected platform's `skills/` directory.

The wheel also includes the docs through `src/rtl_buddy/docs` and `pyproject.toml` `force-include`, so `rb docs` matches the installed version. Keep docs excluded from package discovery to avoid packaging them twice.

Installation uses these contracts:

- `SKILL_DIRNAMES` lists every family member.
- Each installed directory matches its skill `name:`.
- `rtl-buddy skill install` refreshes the family; `status` compares `.rtl_buddy_skill_version`; `uninstall` removes managed copies.
- `src/rtl_buddy/skill/gitignore_snippet.txt` supplies project-install and `print-gitignore` output.
- User scope is the default. `--project` and `--root PATH` create project copies that override user copies.
- Install, status, uninstall, version markers, gitignore handling, and managed obsolete-directory cleanup cover the same family membership.

Do not change the default scope without an explicit policy decision. When membership changes, update the lifecycle tests with the source files and constants.
