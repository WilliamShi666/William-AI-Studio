"""
Unit tests for qa_service.py — model configuration, message building, and provider handling.

TDD: These tests are written BEFORE the implementation changes.
Run with: pytest test_qa_service.py -v
"""
import json
import os
import sys
import pytest

# Ensure the chat module is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa_service import QAService, ModelConfig, _build_models, _env_value


class TestModelConfig:
    """Tests for the ModelConfig dataclass."""

    def test_model_config_defaults(self):
        """ModelConfig should have correct default values."""
        mc = ModelConfig(
            key="test-key",
            model_id="test-model-id",
            display_name="Test Model",
            provider="dashscope",
        )
        assert mc.key == "test-key"
        assert mc.model_id == "test-model-id"
        assert mc.display_name == "Test Model"
        assert mc.provider == "dashscope"
        assert mc.supports_pdf is False
        assert mc.supports_thinking is False
        assert mc.supports_vision is False

    def test_model_config_with_vision(self):
        """ModelConfig should support vision flag."""
        mc = ModelConfig(
            key="vision-model",
            model_id="v-model",
            display_name="Vision",
            provider="dashscope",
            supports_vision=True,
            supports_thinking=True,
            supports_pdf=True,
        )
        assert mc.supports_vision is True
        assert mc.supports_thinking is True
        assert mc.supports_pdf is True


class TestBuildModels:
    """Tests for the _build_models() function."""

    def test_returns_dict(self):
        """_build_models should return a dict."""
        models = _build_models()
        assert isinstance(models, dict)

    def test_exactly_four_models(self):
        """Should have exactly 4 vision-capable models."""
        models = _build_models()
        assert len(models) == 4, f"Expected 4 models, got {len(models)}: {list(models.keys())}"

    def test_all_models_are_vision_capable(self):
        """All models in the QA service should support vision."""
        models = _build_models()
        for key, model in models.items():
            assert model.supports_vision, f"Model '{key}' should support vision"

    def test_all_models_are_dashscope_provider(self):
        """All QA models should use dashscope provider."""
        models = _build_models()
        for key, model in models.items():
            assert model.provider == "dashscope", f"Model '{key}' should be dashscope, got {model.provider}"

    def test_all_models_support_thinking(self):
        """All QA models should support thinking mode."""
        models = _build_models()
        for key, model in models.items():
            assert model.supports_thinking, f"Model '{key}' should support thinking"

    def test_kimi_k2_6_is_first_default(self):
        """kimi-k2.6 should be the first model (default)."""
        models = _build_models()
        keys = list(models.keys())
        assert keys[0] == "kimi-k2.6", f"Default model should be kimi-k2.6, got {keys[0]}"

    def test_expected_model_keys(self):
        """Should contain the expected 4 model keys."""
        models = _build_models()
        expected_keys = {"kimi-k2.6", "qwen3.7-plus", "qwen3.6-35b-a3b", "qwen3.6-27b"}
        actual_keys = set(models.keys())
        assert actual_keys == expected_keys, f"Expected {expected_keys}, got {actual_keys}"

    def test_no_openrouter_models(self):
        """No model should use openrouter provider."""
        models = _build_models()
        for key, model in models.items():
            assert model.provider != "openrouter", f"Model '{key}' should not be openrouter"

    def test_no_gemini_model(self):
        """Gemini model should not be present."""
        models = _build_models()
        assert "gemini" not in models, "Gemini model should be removed"

    def test_no_glm_model(self):
        """GLM models should not be present."""
        models = _build_models()
        assert "glm-4.6v" not in models, "GLM-4.6V model should be removed"

    def test_no_old_qwen_vl_models(self):
        """Old Qwen VL models should not be present."""
        models = _build_models()
        for old_key in ["qwen3-vl-instruct", "qwen3-vl-thinking", "qwen3.5-plus"]:
            assert old_key not in models, f"Old model '{old_key}' should be removed"


class TestQAService:
    """Tests for the QAService class."""

    def test_list_models_returns_list(self):
        """list_models should return a list of dicts."""
        service = QAService()
        models = service.list_models()
        assert isinstance(models, list)
        assert len(models) == 4

    def test_list_models_has_required_fields(self):
        """Each model dict should have all required fields."""
        service = QAService()
        models = service.list_models()
        required_fields = {"key", "model_id", "display_name", "provider",
                           "supports_pdf", "supports_thinking", "supports_vision"}
        for model in models:
            missing = required_fields - set(model.keys())
            assert not missing, f"Model {model.get('key')} missing fields: {missing}"

    def test_list_models_supports_vision(self):
        """All models in list_models should have supports_vision=True."""
        service = QAService()
        models = service.list_models()
        for model in models:
            assert model["supports_vision"] is True, \
                f"Model {model['key']} should have supports_vision=True"

    def test_get_model_valid_key(self):
        """get_model should return ModelConfig for valid key."""
        service = QAService()
        model = service.get_model("kimi-k2.6")
        assert model is not None
        assert model.key == "kimi-k2.6"

    def test_get_model_invalid_key(self):
        """get_model should return None for invalid key."""
        service = QAService()
        model = service.get_model("nonexistent-model")
        assert model is None

    def test_get_model_gemini_removed(self):
        """get_model('gemini') should return None."""
        service = QAService()
        model = service.get_model("gemini")
        assert model is None


class TestBuildMessages:
    """Tests for build_messages and build_content."""

    def test_build_content_text_only(self):
        """build_content with text only returns text block."""
        content = QAService.build_content("Hello")
        assert len(content) == 1
        assert content[0] == {"type": "text", "text": "Hello"}

    def test_build_content_with_images(self):
        """build_content adds image_url blocks for images."""
        content = QAService.build_content(
            "Look at this",
            images=["data:image/jpeg;base64,abc123"],
        )
        assert len(content) == 2
        assert content[0] == {"type": "text", "text": "Look at this"}
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,abc123"},
        }

    def test_build_content_adds_data_prefix(self):
        """build_content prepends data URI prefix for raw base64 images."""
        content = QAService.build_content(
            "Check this",
            images=["abc123"],
        )
        assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,abc123"

    def test_build_content_with_files(self):
        """build_content adds file blocks."""
        content = QAService.build_content(
            "Read this PDF",
            files=[{"filename": "doc.pdf", "data": "base64data"}],
        )
        assert len(content) == 2
        assert content[1]["type"] == "file"
        assert content[1]["file"]["filename"] == "doc.pdf"

    def test_build_messages_deepseek_strips_reasoning(self):
        """build_messages with deepseek provider strips reasoning_content from assistant messages."""
        history = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Hi there!",
                "reasoning_content": "The user is greeting me...",
            },
            {"role": "user", "content": "How are you?"},
        ]
        messages = QAService.build_messages(history, provider="deepseek")
        assert len(messages) == 3
        # Assistant message should NOT have reasoning_content
        assert messages[1]["role"] == "assistant"
        assert "reasoning_content" not in messages[1]
        assert messages[1]["content"] == "Hi there!"

    def test_build_messages_dashscope_preserves_reasoning(self):
        """build_messages with dashscope provider preserves reasoning_content."""
        history = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Hi there!",
                "reasoning_content": "The user is greeting me...",
            },
            {"role": "user", "content": "How are you?"},
        ]
        messages = QAService.build_messages(history, provider="dashscope")
        assert len(messages) == 3
        # Assistant message SHOULD have reasoning_content
        assert messages[1]["role"] == "assistant"
        assert messages[1].get("reasoning_content") == "The user is greeting me..."
        assert messages[1]["content"] == "Hi there!"

    def test_build_messages_deepseek_no_reasoning_field(self):
        """build_messages deepseek: when assistant has no reasoning_content, it still works."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        messages = QAService.build_messages(history, provider="deepseek")
        assert len(messages) == 2
        assert messages[1] == {"role": "assistant", "content": "Hi!"}

    def test_build_messages_user_with_images(self):
        """build_messages constructs multimodal content for user messages with images."""
        history = [
            {
                "role": "user",
                "content": "What is this?",
                "images": json.dumps(["data:image/jpeg;base64,xyz"]),
            },
            {"role": "assistant", "content": "That's a cat."},
        ]
        messages = QAService.build_messages(history, provider="dashscope")
        assert len(messages) == 2
        # User message should have content array
        assert isinstance(messages[0]["content"], list)
        assert messages[0]["content"][0]["type"] == "text"

    def test_build_messages_images_as_list(self):
        """build_messages handles images already as a list (not JSON string)."""
        history = [
            {
                "role": "user",
                "content": "Analyze",
                "images": ["data:image/png;base64,abc"],
            },
        ]
        messages = QAService.build_messages(history, provider="dashscope")
        assert isinstance(messages[0]["content"], list)
        assert len(messages[0]["content"]) == 2  # text + image

    def test_build_messages_default_provider(self):
        """build_messages should default to dashscope provider."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi", "reasoning_content": "thinking..."},
        ]
        messages = QAService.build_messages(history)
        # Default provider is dashscope, which preserves reasoning
        assert "reasoning_content" in messages[1]


class TestExtractReasoning:
    """Tests for _extract_reasoning static method."""

    def test_extract_reasoning_content_string(self):
        """Should extract reasoning_content as string."""
        class FakeDelta:
            reasoning_content = "I am thinking..."
            content = None
        result = QAService._extract_reasoning(FakeDelta())
        assert result == "I am thinking..."

    def test_extract_openrouter_reasoning(self):
        """Should extract 'reasoning' field (OpenRouter format)."""
        class FakeDelta:
            reasoning_content = None
            reasoning = "OpenRouter reasoning"
        result = QAService._extract_reasoning(FakeDelta())
        assert result == "OpenRouter reasoning"

    def test_extract_reasoning_dict(self):
        """Should extract from dict-format reasoning_content."""
        class FakeDelta:
            reasoning_content = None
            reasoning = {"content": "Dict-based reasoning"}
        result = QAService._extract_reasoning(FakeDelta())
        assert result == "Dict-based reasoning"

    def test_extract_reasoning_none(self):
        """Should return None when no reasoning present."""
        class FakeDelta:
            reasoning_content = None
            reasoning = None
        result = QAService._extract_reasoning(FakeDelta())
        assert result is None


class TestEnvValue:
    """Tests for _env_value helper."""

    def test_env_value_returns_default(self):
        """Should return default when env var not set."""
        os.environ.pop("NONEXISTENT_VAR_TEST", None)
        result = _env_value("NONEXISTENT_VAR_TEST", "fallback")
        assert result == "fallback"

    def test_env_value_strips_quotes(self):
        """Should strip surrounding quotes."""
        os.environ["TEST_QUOTED_VAR"] = '"quoted-value"'
        result = _env_value("TEST_QUOTED_VAR", "default")
        assert result == "quoted-value"
        os.environ.pop("TEST_QUOTED_VAR", None)
