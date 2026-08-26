## Prerequisites

- Python 3.11 or later (matches the floor in `pyproject.toml`).
- `uv` — see <https://docs.astral.sh/uv/> for install instructions.
- `git`.

`uv` owns the project environment. The repo uses `pyproject.toml` plus a committed `uv.lock`; do not maintain a hand-rolled `requirements.txt`.

External EDA tools (Verilator, Yosys, Verible, OpenROAD, etc.) are only required when running the matching `rb` subcommand. Day-to-day Python and docs work needs none of them. See [Installation](https://rtl-buddy.github.io/rtl_buddy/v5/install/#external-tools-by-feature) for the full feature-to-dependency matrix.
