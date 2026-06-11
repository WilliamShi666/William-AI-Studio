from __future__ import annotations

import os
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from openai import AsyncOpenAI


@dataclass(frozen=True)
class ModelConfig:
    key: str
    model_id: str
    display_name: str
    provider: str
    supports_pdf: bool = False
    supports_thinking: bool = False
    supports_vision: bool = False


def _env_value(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().strip("\"").strip("'")


def _build_models() -> dict[str, ModelConfig]:
    return {
        # ========== 视觉模型（2026年6月更新）==========
        # kimi-k2.6 作为默认模型（视觉+思考）
        "kimi-k2.6": ModelConfig(
            key="kimi-k2.6",
            model_id="kimi-k2.6",
            display_name="Kimi K2.6",
            provider="dashscope",
            supports_pdf=True,
            supports_thinking=True,
            supports_vision=True,
        ),
        "qwen3.7-plus": ModelConfig(
            key="qwen3.7-plus",
            model_id="qwen3.7-plus",
            display_name="Qwen 3.7 Plus",
            provider="dashscope",
            supports_pdf=True,
            supports_thinking=True,
            supports_vision=True,
        ),
        "qwen3.6-35b-a3b": ModelConfig(
            key="qwen3.6-35b-a3b",
            model_id="qwen3.6-35b-a3b",
            display_name="Qwen 3.6 35B A3B",
            provider="dashscope",
            supports_pdf=True,
            supports_thinking=True,
            supports_vision=True,
        ),
        "qwen3.6-27b": ModelConfig(
            key="qwen3.6-27b",
            model_id="qwen3.6-27b",
            display_name="Qwen 3.6 27B",
            provider="dashscope",
            supports_pdf=True,
            supports_thinking=True,
            supports_vision=True,
        ),
    }


class QAService:
    def __init__(self) -> None:
        self._models = _build_models()
        self._dashscope_key = os.getenv("DASHSCOPE_API_KEY", "")
        self._deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")
        self._dashscope_client = AsyncOpenAI(
            api_key=self._dashscope_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        # DeepSeek client: currently all QA models are dashscope vision models,
        # but deepseek support is pre-built for forward-compatibility if a
        # deepseek model is added to _build_models() in the future.
        self._deepseek_client = AsyncOpenAI(
            api_key=self._deepseek_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "key": model.key,
                "model_id": model.model_id,
                "display_name": model.display_name,
                "provider": model.provider,
                "supports_pdf": model.supports_pdf,
                "supports_thinking": model.supports_thinking,
                "supports_vision": model.supports_vision,
            }
            for model in self._models.values()
        ]

    def get_model(self, key: str) -> Optional[ModelConfig]:
        return self._models.get(key)

    @staticmethod
    def build_content(
        text: str,
        images: Optional[list[str]] = None,
        files: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]

        if images:
            for image in images:
                url = image
                if image and not image.startswith("data:") and not image.startswith("http"):
                    url = f"data:image/jpeg;base64,{image}"
                content.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })

        if files:
            for file in files:
                filename = str(file.get("filename") or "document.pdf")
                data = str(file.get("data") or "")
                if data and not data.startswith("data:"):
                    data = f"data:application/pdf;base64,{data}"
                content.append({
                    "type": "file",
                    "file": {
                        "filename": filename,
                        "file_data": data,
                    },
                })

        return content

    @staticmethod
    def build_messages(
        history: list[dict[str, Any]],
        provider: str = "dashscope",
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for msg in history:
            role = msg.get("role")
            content = msg.get("content", "")
            images = msg.get("images")
            files = msg.get("files")
            if isinstance(images, str):
                try:
                    images = json.loads(images)
                except json.JSONDecodeError:
                    images = None
            if isinstance(files, str):
                try:
                    files = json.loads(files)
                except json.JSONDecodeError:
                    files = None
            if role == "user" and (images or files):
                messages.append({
                    "role": role,
                    "content": QAService.build_content(content, images=images, files=files),
                })
            elif role == "assistant" and provider == "deepseek":
                # DeepSeek: strip reasoning_content from history
                # (without tool calls, API ignores it)
                messages.append({"role": role, "content": content})
            else:
                # DashScope / other providers: preserve reasoning_content
                reasoning = msg.get("reasoning_content")
                if reasoning:
                    messages.append({
                        "role": role,
                        "content": content,
                        "reasoning_content": reasoning,
                    })
                else:
                    messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _extract_reasoning(delta: Any) -> Optional[str]:
        for attr in ("reasoning_content", "reasoning"):
            value = getattr(delta, attr, None)
            if not value:
                continue
            if isinstance(value, dict):
                text = value.get("content") or value.get("text") or value.get("details")
                if text:
                    return str(text)
            if isinstance(value, str):
                return value
        return None

    async def chat_stream(
        self,
        model_key: str,
        messages: list[dict[str, Any]],
        enable_thinking: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        model = self.get_model(model_key)
        if not model:
            yield {"type": "error", "data": f"unknown model: {model_key}"}
            return

        if model.provider == "dashscope" and not self._dashscope_key:
            yield {"type": "error", "data": "DASHSCOPE_API_KEY 未配置"}
            return
        if model.provider == "deepseek" and not self._deepseek_key:
            yield {"type": "error", "data": "DEEPSEEK_API_KEY 未配置"}
            return

        client = self._dashscope_client if model.provider == "dashscope" else self._deepseek_client
        extra_body = None
        if enable_thinking and model.supports_thinking:
            if model.provider == "deepseek":
                extra_body = {"thinking": {"type": "enabled"}}
            else:
                # dashscope
                extra_body = {"enable_thinking": True, "thinking_budget": 81920}

        request_kwargs = {
            "model": model.model_id,
            "messages": messages,
            "stream": True,
        }
        if extra_body:
            request_kwargs["extra_body"] = extra_body

        stream = await client.chat.completions.create(**request_kwargs)

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = self._extract_reasoning(delta)
            if reasoning:
                yield {"type": "reasoning", "data": reasoning}
            content = getattr(delta, "content", None)
            if content:
                yield {"type": "content", "data": content}
