## Diagnose view errors

Failed `GET /view.json` requests return JSON with `error.kind`. Branch on the kind, not the prose:

| Kind | Meaning | Recovery |
| --- | --- | --- |
| `view_generation_failed` | Filelist, parse, or elaboration failed. | Read `log_tail` or `log_path`, fix the model, then restart or request it again. |
| `unknown_model` | No unique matching model exists. | Correct the name or pass `--models-file`. |
| `no_active_model` | No model or prebuilt view is selected. | Request `?model=NAME` or start with `--model`. |
| `no_project_root` | The hub cannot discover project configuration. | Start inside the project or provide the correct root context. |

`view_generation_failed` includes the final renderer log lines. Common causes are unsupported parser syntax, missing submodules, and filelist entries the renderer cannot consume.
