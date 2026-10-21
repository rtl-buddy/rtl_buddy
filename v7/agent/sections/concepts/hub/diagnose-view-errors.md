## Diagnose view errors

A failed `GET /view.json` returns JSON with `error.kind`. Branch on the kind, not the prose:

| Kind | Meaning | Recovery |
| --- | --- | --- |
| `view_generation_failed` | Filelist, parse or elaboration failed. | Read `log_tail` or `log_path`, fix the model, then request it again. |
| `unknown_model` | No unique matching model exists. | Correct the name or pass `--models-file`. |
| `no_active_model` | No model or prebuilt view is selected. | Request `?model=NAME` or start with `--model`. |
| `no_project_root` | The hub cannot find project configuration. | Start inside the project. |
