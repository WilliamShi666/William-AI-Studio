from services.shadow_clone_model_config import (
    normalize_shadow_clone_selected_model_name,
)


def test_shadow_clone_model_policy_accepts_official_kimi_aliases() -> None:
    assert normalize_shadow_clone_selected_model_name("kimi-k2.5") == "kimi-k2.5"
    assert (
        normalize_shadow_clone_selected_model_name("moonshotai/kimi-k2.5")
        == "kimi-k2.5"
    )
    assert normalize_shadow_clone_selected_model_name("kimi-k2.6") == "kimi-k2.6"
    assert (
        normalize_shadow_clone_selected_model_name("moonshotai/kimi-k2.6")
        == "kimi-k2.6"
    )


def test_shadow_clone_model_policy_accepts_official_deepseek_variants() -> None:
    for model_name in (
        "deepseek-v4-pro-high",
        "deepseek-v4-pro-max",
        "deepseek-v4-flash-high",
        "deepseek-v4-flash-max",
    ):
        assert normalize_shadow_clone_selected_model_name(model_name) == model_name


def test_shadow_clone_model_policy_accepts_openrouter_models() -> None:
    assert (
        normalize_shadow_clone_selected_model_name(
            "openrouter/minimax/minimax-m2.5"
        )
        == "openrouter/minimax/minimax-m2.5"
    )
    assert (
        normalize_shadow_clone_selected_model_name(
            "openrouter/moonshotai/kimi-k2.5"
        )
        == "openrouter/moonshotai/kimi-k2.5"
    )
    for model_name in (
        "openrouter/moonshotai/kimi-k2.6",
        "openrouter/xiaomi/mimo-v2.5-pro",
        "openrouter/deepseek/deepseek-v4-pro",
        "openrouter/deepseek/deepseek-v4-flash",
    ):
        assert normalize_shadow_clone_selected_model_name(model_name) == model_name


def test_shadow_clone_model_policy_rejects_non_openrouter_and_qwen_variants() -> None:
    for model_name in (
        "dashscope/qwen3.5-plus",
        "openrouter/qwen/qwen3.5-plus",
        "ppio/moonshotai/kimi-k2.5",
        "volcengine/doubao-seed-2-0-pro-260215",
    ):
        try:
            normalize_shadow_clone_selected_model_name(model_name)
        except ValueError:
            continue
        raise AssertionError(f"{model_name} should be rejected for Shadow Clone")
