## Model names and tops must be distinct

Every selected model needs a distinct `name`, and every exported model needs a distinct top module. `graph build` refuses a violation and names both models and both `models.yaml` files. Rename one of two models with the same name; `graph: false` does not help. For two models with the same top, give one a different `top:` or set `graph: false` on it.
