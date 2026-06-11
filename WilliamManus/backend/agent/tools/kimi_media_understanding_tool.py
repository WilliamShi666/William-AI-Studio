import base64
import hashlib
import os
import posixpath
from typing import Any, Dict, Optional, Tuple

from openai import OpenAI

from agentpress.tool import ToolResult
from utils.config import config
from utils.logger import logger


DEFAULT_PROMPT = "请描述该媒体内容，提取关键信息；如有文字请转写。"
DEFAULT_MODEL = "kimi-k2.6"
DEFAULT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_VIDEO_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_OUTPUT_CHARS = 4000


def normalize_workspace_path(path: str) -> str:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("Path must stay within /workspace")
    if raw_path.startswith("/") and raw_path != "/workspace" and not raw_path.startswith("/workspace/"):
        raise ValueError("Path must stay within /workspace")

    relative_path = raw_path[len("/workspace") :].lstrip("/") if raw_path == "/workspace" or raw_path.startswith("/workspace/") else raw_path
    normalized = posixpath.normpath(posixpath.join("/workspace", relative_path))
    if normalized != "/workspace" and normalized.startswith("/workspace/"):
        return normalized
    raise ValueError("Path must stay within /workspace")


def detect_media_mime(data: bytes, path: str = "") -> Tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "video", "video/x-msvideo"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        extension = posixpath.splitext(path)[1].lower()
        if extension == ".webm":
            return "video", "video/webm"
        return "video", "video/x-matroska"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        major_brand = data[8:12]
        if major_brand == b"qt  ":
            return "video", "video/quicktime"
        return "video", "video/mp4"

    raise ValueError(f"Unsupported media type for {path or 'file'}")


def build_data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _extract_summary(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


def _extract_usage(response: Any) -> Optional[Dict[str, Any]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    return {
        key: getattr(usage, key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(usage, key, None) is not None
    }


class KimiMediaUnderstandingTool:
    def __init__(
        self,
        *,
        code_tool: Any,
        openai_client_factory: Any = None,
        image_max_bytes: Optional[int] = None,
        video_max_bytes: Optional[int] = None,
    ) -> None:
        self.code_tool = code_tool
        self.openai_client_factory = openai_client_factory or OpenAI
        self.image_max_bytes = image_max_bytes or int(
            getattr(config, "KIMI_MEDIA_IMAGE_MAX_BYTES", DEFAULT_IMAGE_MAX_BYTES)
            or DEFAULT_IMAGE_MAX_BYTES
        )
        self.video_max_bytes = video_max_bytes or int(
            getattr(config, "KIMI_MEDIA_VIDEO_MAX_BYTES", DEFAULT_VIDEO_MAX_BYTES)
            or DEFAULT_VIDEO_MAX_BYTES
        )

    async def understand_media(
        self,
        path: str,
        media_type: str = "auto",
        prompt: str = DEFAULT_PROMPT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        detail_level: str = "normal",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ToolResult:
        try:
            clean_path = normalize_workspace_path(path)
            requested_media_type = str(media_type or "auto").strip().lower()
            if requested_media_type not in {"auto", "image", "video"}:
                raise ValueError("media_type must be one of: auto, image, video")

            data = await self._download_bytes(clean_path)
            detected_media_type, mime_type = detect_media_mime(data, clean_path)
            if requested_media_type != "auto" and requested_media_type != detected_media_type:
                raise ValueError(
                    f"Requested media_type {requested_media_type} does not match detected {detected_media_type}",
                )

            limit = self.image_max_bytes if detected_media_type == "image" else self.video_max_bytes
            if len(data) > limit:
                raise ValueError(
                    f"{detected_media_type} file exceeds limit: {len(data)} bytes > {limit} bytes",
                )

            summary, model, usage = self._call_kimi(
                data=data,
                detected_media_type=detected_media_type,
                mime_type=mime_type,
                prompt=prompt,
                detail_level=detail_level,
                max_output_chars=max_output_chars,
                timeout_seconds=timeout_seconds,
            )
            max_chars = _clamp_int(max_output_chars, default=DEFAULT_MAX_OUTPUT_CHARS, minimum=200, maximum=12000)
            truncated = len(summary) > max_chars

            return ToolResult(
                success=True,
                output={
                    "success": True,
                    "path": clean_path,
                    "media_type": detected_media_type,
                    "mime_type": mime_type,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "model": model,
                    "summary": summary[:max_chars],
                    "truncated": truncated,
                    "usage": usage,
                },
            )
        except Exception as exc:
            logger.warning("Kimi media understanding failed: %s", exc)
            return ToolResult(
                success=False,
                output={"success": False, "path": path, "error": str(exc)},
            )

    async def _download_bytes(self, clean_path: str) -> bytes:
        result = await self.code_tool.download_file(path=clean_path, as_base64=True)
        if not getattr(result, "success", False):
            output = getattr(result, "output", {}) or {}
            if isinstance(output, dict):
                raise RuntimeError(str(output.get("error") or "Failed to download media file"))
            raise RuntimeError(str(output or "Failed to download media file"))

        output = getattr(result, "output", {}) or {}
        if not isinstance(output, dict) or not output.get("base64"):
            raise RuntimeError("download_file did not return base64 media content")
        return base64.b64decode(str(output["base64"]), validate=True)

    def _call_kimi(
        self,
        *,
        data: bytes,
        detected_media_type: str,
        mime_type: str,
        prompt: str,
        detail_level: str,
        max_output_chars: int,
        timeout_seconds: int,
    ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        api_key = os.getenv("MOONSHOT_API_KEY") or getattr(config, "MOONSHOT_API_KEY", None)
        if not api_key:
            raise RuntimeError("MOONSHOT_API_KEY is required for understand_media")

        model = os.getenv("KIMI_MEDIA_MODEL") or getattr(config, "KIMI_MEDIA_MODEL", None) or DEFAULT_MODEL
        base_url = (
            os.getenv("MOONSHOT_BASE_URL")
            or os.getenv("MOONSHOT_API_BASE")
            or getattr(config, "MOONSHOT_BASE_URL", None)
            or getattr(config, "MOONSHOT_API_BASE", None)
        )
        timeout = _clamp_int(timeout_seconds, default=DEFAULT_TIMEOUT_SECONDS, minimum=5, maximum=120)
        max_chars = _clamp_int(max_output_chars, default=DEFAULT_MAX_OUTPUT_CHARS, minimum=200, maximum=12000)
        detail = str(detail_level or "normal").strip().lower()
        if detail not in {"brief", "normal", "detailed"}:
            detail = "normal"

        client = self.openai_client_factory(api_key=api_key, base_url=base_url, timeout=timeout)
        media_url = build_data_url(data, mime_type)
        media_block_key = "image_url" if detected_media_type == "image" else "video_url"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"{prompt or DEFAULT_PROMPT}\n"
                                f"Detail level: {detail}. Return at most {max_chars} characters."
                            ),
                        },
                        {
                            "type": media_block_key,
                            media_block_key: {"url": media_url},
                        },
                    ],
                },
            ],
            max_tokens=max(256, min(4096, max_chars // 2)),
            extra_body={"thinking": {"type": "disabled"}},
            timeout=timeout,
        )
        return _extract_summary(response), str(model), _extract_usage(response)
