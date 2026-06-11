import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest

from agentpress.tool import ToolResult
from agent.tools.kimi_media_understanding_tool import (
    KimiMediaUnderstandingTool,
    build_data_url,
    detect_media_mime,
    normalize_workspace_path,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
AVI_BYTES = b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 16
MKV_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 16


def _content_from_text(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5),
    )


class FakeOpenAIClient:
    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )
        self.calls = []

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return _content_from_text("这是一张测试图片。")


class FakeCodeTool:
    def __init__(self, data):
        self.data = data
        self.paths = []

    async def download_file(self, path, as_base64=False):
        self.paths.append((path, as_base64))
        return ToolResult(
            success=True,
            output={
                "path": path,
                "base64": base64.b64encode(self.data).decode("ascii"),
                "bytes": len(self.data),
            },
        )


def test_normalize_workspace_path_rejects_traversal_and_host_paths():
    assert normalize_workspace_path("demo.png") == "/workspace/demo.png"
    assert normalize_workspace_path("/workspace/nested/demo.png") == "/workspace/nested/demo.png"

    with pytest.raises(ValueError, match="/workspace"):
        normalize_workspace_path("../secret.png")

    with pytest.raises(ValueError, match="/workspace"):
        normalize_workspace_path("/etc/passwd")

    with pytest.raises(ValueError, match="/workspace"):
        normalize_workspace_path("/workspace_evil/demo.png")


def test_detect_media_mime_uses_magic_bytes_not_extension_hint():
    assert detect_media_mime(PNG_BYTES, "/workspace/not-really.jpg") == ("image", "image/png")
    assert detect_media_mime(MP4_BYTES, "/workspace/movie.bin") == ("video", "video/mp4")
    assert detect_media_mime(AVI_BYTES, "/workspace/movie.bin") == ("video", "video/x-msvideo")
    assert detect_media_mime(MKV_BYTES, "/workspace/movie.bin") == ("video", "video/x-matroska")

    with pytest.raises(ValueError, match="Unsupported"):
        detect_media_mime(b"plain text", "/workspace/demo.png")


def test_build_data_url_for_image_and_video():
    assert build_data_url(PNG_BYTES, "image/png").startswith("data:image/png;base64,")
    assert build_data_url(MP4_BYTES, "video/mp4").startswith("data:video/mp4;base64,")


def test_understand_media_calls_openai_compatible_client_without_network(monkeypatch):
    fake_client = FakeOpenAIClient()
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setattr(
        "agent.tools.kimi_media_understanding_tool.OpenAI",
        lambda **kwargs: fake_client,
    )

    tool = KimiMediaUnderstandingTool(code_tool=FakeCodeTool(PNG_BYTES))

    result = asyncio.run(
        tool.understand_media(
            path="demo.png",
            media_type="auto",
            prompt="描述图片",
            max_output_chars=4000,
            detail_level="normal",
            timeout_seconds=9,
        )
    )

    assert result.success is True
    assert result.output["success"] is True
    assert result.output["path"] == "/workspace/demo.png"
    assert result.output["media_type"] == "image"
    assert result.output["mime_type"] == "image/png"
    assert result.output["bytes"] == len(PNG_BYTES)
    assert result.output["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert result.output["model"] == "kimi-k2.6"
    assert result.output["summary"] == "这是一张测试图片。"
    assert result.output["truncated"] is False

    call = fake_client.calls[0]
    assert call["model"] == "kimi-k2.6"
    assert call["timeout"] == 9
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "描述图片" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_understand_media_builds_video_payload(monkeypatch):
    fake_client = FakeOpenAIClient()
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setattr(
        "agent.tools.kimi_media_understanding_tool.OpenAI",
        lambda **kwargs: fake_client,
    )

    tool = KimiMediaUnderstandingTool(code_tool=FakeCodeTool(MP4_BYTES))
    result = asyncio.run(tool.understand_media(path="/workspace/demo.mp4"))

    assert result.success is True
    assert result.output["media_type"] == "video"
    assert result.output["mime_type"] == "video/mp4"
    content = fake_client.calls[0]["messages"][0]["content"]
    assert content[1]["type"] == "video_url"
    assert content[1]["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_understand_media_enforces_media_type_and_size_limits(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setattr(
        "agent.tools.kimi_media_understanding_tool.OpenAI",
        lambda **kwargs: FakeOpenAIClient(),
    )

    mismatched = KimiMediaUnderstandingTool(code_tool=FakeCodeTool(PNG_BYTES))
    mismatch_result = asyncio.run(mismatched.understand_media(path="demo.png", media_type="video"))
    assert mismatch_result.success is False
    assert "does not match" in mismatch_result.output["error"]

    oversized = KimiMediaUnderstandingTool(
        code_tool=FakeCodeTool(PNG_BYTES),
        image_max_bytes=len(PNG_BYTES) - 1,
    )
    size_result = asyncio.run(oversized.understand_media(path="demo.png"))
    assert size_result.success is False
    assert "exceeds" in size_result.output["error"]
