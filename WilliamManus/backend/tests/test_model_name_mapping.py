import ast
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve().parents[1] / "agent" / "run.py"


def _extract_model_mapping() -> dict[str, str]:
    source = RUNNER_PATH.read_text()
    module = ast.parse(source)

    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "AgentRunner":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_map_model_name":
                    for stmt in item.body:
                        if isinstance(stmt, ast.Assign):
                            for target in stmt.targets:
                                if isinstance(target, ast.Name) and target.id == "model_mapping":
                                    return ast.literal_eval(stmt.value)

    raise AssertionError("AgentRunner._map_model_name model_mapping not found")


def test_maps_openrouter_kimi_to_dedicated_agentscope_model_key() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/moonshotai/kimi-k2.5"] == "openrouter-kimi-k2.5"
    assert mapping["openrouter/moonshotai/kimi-k2.6"] == "openrouter-kimi-k2.6"


def test_maps_ppio_kimi_to_dedicated_agentscope_model_key() -> None:
    mapping = _extract_model_mapping()
    assert mapping["ppio/moonshotai/kimi-k2.5"] == "ppio-kimi-k2.5"


def test_maps_gemini_31_variants_to_agentscope_model_key() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/google/gemini-3.1-pro-preview"] == "gemini-3-pro"
    assert mapping["google/gemini-3.1-pro-preview"] == "gemini-3-pro"
    assert mapping["gemini/gemini-3.1-pro-preview"] == "gemini-3-pro"
    assert "openrouter/google/gemini-3-pro-preview" not in mapping
    assert "google/gemini-3-pro-preview" not in mapping
    assert "gemini/gemini-3-pro-preview" not in mapping


def test_openrouter_kimi_partial_match_precedes_generic_kimi_match() -> None:
    source = RUNNER_PATH.read_text()
    openrouter_branch = 'elif "openrouter" in model_lower and "kimi-k2.5" in model_lower:'
    generic_kimi_branch = 'elif "kimi" in model_lower or "moonshot" in model_lower:'

    assert openrouter_branch in source
    assert generic_kimi_branch in source
    assert source.index(openrouter_branch) < source.index(generic_kimi_branch)


def test_new_deepseek_v4_partial_matches_precede_legacy_deepseek_fallback() -> None:
    source = RUNNER_PATH.read_text()
    deepseek_v4_pro_branch = 'elif "deepseek-v4-pro" in model_lower:'
    deepseek_v4_flash_branch = 'elif "deepseek-v4-flash" in model_lower:'
    legacy_deepseek_branch = 'elif "deepseek" in model_lower:'

    assert deepseek_v4_pro_branch in source
    assert deepseek_v4_flash_branch in source
    assert legacy_deepseek_branch in source
    assert source.index(deepseek_v4_pro_branch) < source.index(legacy_deepseek_branch)
    assert source.index(deepseek_v4_flash_branch) < source.index(legacy_deepseek_branch)


def test_ppio_kimi_partial_match_precedes_generic_kimi_match() -> None:
    source = RUNNER_PATH.read_text()
    ppio_branch = 'elif "ppio" in model_lower and "kimi-k2.5" in model_lower:'
    generic_kimi_branch = 'elif "kimi" in model_lower or "moonshot" in model_lower:'

    assert ppio_branch in source
    assert generic_kimi_branch in source
    assert source.index(ppio_branch) < source.index(generic_kimi_branch)


def test_maps_openrouter_qwen35_to_agentscope_model_key() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/qwen/qwen3.5-plus"] == "qwen3.5-plus"
    assert mapping["qwen/qwen3.5-plus"] == "qwen3.5-plus"
    assert mapping["dashscope/qwen3.5-plus"] == "qwen3.5-plus"
    assert mapping["openrouter/qwen/qwen3.5-397b-a17b"] == "qwen3.5-plus"
    assert mapping["qwen/qwen3.5-397b-a17b"] == "qwen3.5-plus"
    assert mapping["dashscope/qwen3.5-397b-a17b"] == "qwen3.5-plus"


def test_maps_glm_51_variants_to_dedicated_agentscope_model_key() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/z-ai/glm-5.1"] == "glm-5.1"
    assert mapping["z-ai/glm-5.1"] == "glm-5.1"
    assert mapping["glm-5.1"] == "glm-5.1"


def test_maps_openrouter_qwen36_to_dedicated_agentscope_model_key() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/qwen/qwen3.6-plus"] == "qwen3.6-plus"
    assert mapping["qwen/qwen3.6-plus"] == "qwen3.6-plus"
    assert mapping["qwen3.6-plus"] == "qwen3.6-plus"


def test_maps_openrouter_and_dashscope_minimax_variants_to_dedicated_model_keys() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/minimax/minimax-m2.5"] == "openrouter-minimax-m2.5"
    assert mapping["minimax/minimax-m2.5"] == "openrouter-minimax-m2.5"
    assert mapping["openrouter/minimax/minimax-m2.7"] == "openrouter-minimax-m2.7"
    assert mapping["minimax/minimax-m2.7"] == "openrouter-minimax-m2.7"
    assert mapping["dashscope/minimax-m2.5"] == "dashscope-minimax-m2.5"
    assert mapping["MiniMax-M2.5"] == "dashscope-minimax-m2.5"


def test_maps_mimo_and_proxy_gpt_variants_to_dedicated_model_keys() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/xiaomi/mimo-v2-pro"] == "mimo-v2-pro"
    assert mapping["xiaomi/mimo-v2-pro"] == "mimo-v2-pro"
    assert mapping["openrouter/xiaomi/mimo-v2.5-pro"] == "mimo-v2.5-pro"
    assert mapping["xiaomi/mimo-v2.5-pro"] == "mimo-v2.5-pro"
    assert mapping["openai-proxy/gpt-5.4"] == "proxy-gpt-5.4"
    assert mapping["mystery-model"] == "proxy-gpt-5.4"
    assert mapping["gpt-5.4"] == "proxy-gpt-5.4"


def test_maps_new_deepseek_v4_variants_to_dedicated_model_keys() -> None:
    mapping = _extract_model_mapping()
    assert mapping["openrouter/deepseek/deepseek-v4-pro"] == "openrouter-deepseek-v4-pro"
    assert mapping["deepseek/deepseek-v4-pro"] == "deepseek-v4-pro-high"
    assert mapping["openrouter/deepseek/deepseek-v4-flash"] == "openrouter-deepseek-v4-flash"
    assert mapping["deepseek/deepseek-v4-flash"] == "deepseek-v4-flash-high"
    assert mapping["deepseek-v4-pro-high"] == "deepseek-v4-pro-high"
    assert mapping["deepseek-v4-pro-max"] == "deepseek-v4-pro-max"
    assert mapping["deepseek-v4-flash-high"] == "deepseek-v4-flash-high"
    assert mapping["deepseek-v4-flash-max"] == "deepseek-v4-flash-max"


def test_qwen_partial_match_branch_exists() -> None:
    source = RUNNER_PATH.read_text()
    assert 'elif "qwen3.5-plus" in model_lower:' in source


def test_qwen_alias_fallback_branch_exists() -> None:
    source = RUNNER_PATH.read_text()
    assert 'elif "qwen" in model_lower:' in source
    assert 'resolution_reason = "qwen_alias_fallback"' in source


def test_glm51_partial_match_precedes_glm5_match() -> None:
    source = RUNNER_PATH.read_text()
    glm51_branch = 'elif "glm-5.1" in model_lower:'
    glm5_branch = 'elif "glm-5" in model_lower:'

    assert glm51_branch in source
    assert glm5_branch in source
    assert source.index(glm51_branch) < source.index(glm5_branch)


def test_qwen36_partial_match_precedes_generic_qwen_fallback() -> None:
    source = RUNNER_PATH.read_text()
    qwen36_branch = 'elif "qwen3.6-plus" in model_lower:'
    generic_qwen_branch = 'elif "qwen" in model_lower:'

    assert qwen36_branch in source
    assert generic_qwen_branch in source
    assert source.index(qwen36_branch) < source.index(generic_qwen_branch)


def test_openrouter_minimax_partial_match_precedes_generic_minimax_match() -> None:
    source = RUNNER_PATH.read_text()
    openrouter_branch = 'elif "openrouter" in model_lower and "minimax" in model_lower and "m2.5" in model_lower:'
    generic_minimax_branch = 'elif "minimax" in model_lower and "m2.5" in model_lower:'

    assert openrouter_branch in source
    assert generic_minimax_branch in source
    assert source.index(openrouter_branch) < source.index(generic_minimax_branch)


def test_dashscope_minimax_partial_match_precedes_generic_minimax_match() -> None:
    source = RUNNER_PATH.read_text()
    dashscope_branch = 'elif "dashscope" in model_lower and "minimax" in model_lower and "m2.5" in model_lower:'
    generic_minimax_branch = 'elif "minimax" in model_lower and "m2.5" in model_lower:'

    assert dashscope_branch in source
    assert generic_minimax_branch in source
    assert source.index(dashscope_branch) < source.index(generic_minimax_branch)


def test_gemini_partial_match_targets_31_identifier() -> None:
    source = RUNNER_PATH.read_text()
    assert 'if "gemini-3.1-pro" in model_lower or "gemini-2.5-pro" in model_lower:' in source
    assert 'if "gemini-3-pro" in model_lower or "gemini-2.5-pro" in model_lower:' not in source


def test_unknown_model_no_longer_defaults_to_gemini() -> None:
    source = RUNNER_PATH.read_text()
    assert "defaulting to gemini-3-flash" not in source
    assert "raise ValueError(" in source
