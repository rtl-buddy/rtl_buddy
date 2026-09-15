from pathlib import Path

import pytest

from rtl_buddy.seeding import (
    MAX_SIM_SEED,
    derive_test_seed,
    suite_seed_identity,
    validate_master_seed,
)


def test_derived_seed_is_stable_and_identity_scoped():
    kwargs = {
        "master_seed": 20260914,
        "suite_identity": "verif/vxp/tests.yaml",
        "test_name": "deepseek_v4.case=fp16",
        "run_id": None,
    }
    first = derive_test_seed(**kwargs)
    assert first == derive_test_seed(**kwargs)
    assert 1 <= first.seed <= MAX_SIM_SEED
    assert first.identity == ("verif/vxp/tests.yaml::deepseek_v4.case=fp16::single")

    changed = {
        derive_test_seed(**{**kwargs, "master_seed": 20260915}).seed,
        derive_test_seed(**{**kwargs, "test_name": "deepseek_v4.case=fp32"}).seed,
        derive_test_seed(**{**kwargs, "run_id": 2}).seed,
    }
    assert first.seed not in changed


def test_suite_identity_is_independent_of_checkout_path(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    relative = Path("verif/vxp/tests.yaml")
    for root in (left, right):
        (root / relative).parent.mkdir(parents=True)
        (root / relative).touch()

    assert suite_seed_identity(str(left / relative), str(left)) == relative.as_posix()
    assert suite_seed_identity(str(right / relative), str(right)) == relative.as_posix()


def test_sweep_expansion_order_does_not_affect_resolved_seeds():
    names = ["deepseek_v4.dtype=fp16", "deepseek_v4.dtype=fp32"]

    def resolve(order):
        return {
            name: derive_test_seed(
                20260914,
                suite_identity="verif/ecp/t1-tests.yaml",
                test_name=name,
                run_id=None,
            ).seed
            for name in order
        }

    assert resolve(names) == resolve(reversed(names))


def test_master_seed_rejects_negative_values():
    with pytest.raises(ValueError, match="nonnegative integer"):
        validate_master_seed(-1)


def test_master_seed_accepts_values_above_signed_64_bit():
    seed = (1 << 96) + 20260914
    assert validate_master_seed(seed) == seed
    assert (
        1
        <= derive_test_seed(
            seed,
            suite_identity="verif/vxp/tests.yaml",
            test_name="deepseek_v4",
            run_id=None,
        ).seed
        <= MAX_SIM_SEED
    )
