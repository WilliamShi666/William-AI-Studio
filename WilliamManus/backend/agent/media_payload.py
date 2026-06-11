"""Helpers for user message media references stored in events.content.parts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class MediaRef:
    """Normalized reference to uploaded media stored in sandbox."""

    kind: str
    path: str
    mime_type: str
    filename: str
    sha256: str = ""

    def to_dict(self) -> Dict[str, str]:
        payload = {
            "kind": self.kind,
            "path": self.path,
            "mime_type": self.mime_type,
            "filename": self.filename,
        }
        if self.sha256:
            payload["sha256"] = self.sha256
        return payload


@dataclass
class UserInputPayload:
    """Parsed user input with optional media references."""

    text: str
    media_refs: List[MediaRef]


def is_image_upload(content_type: str, filename: str) -> bool:
    content_type = (content_type or "").strip().lower()
    if content_type.startswith("image/"):
        return True

    lowered = (filename or "").strip().lower()
    return lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"))


def normalize_media_ref(raw: Any) -> Optional[MediaRef]:
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("kind") or "").strip().lower()
    if kind != "image":
        return None

    path = str(raw.get("path") or "").strip()
    if not path or not path.startswith("/workspace/"):
        return None

    filename = str(raw.get("filename") or "").strip()
    if not filename:
        filename = os.path.basename(path) or "uploaded_image"

    mime_type = str(raw.get("mime_type") or "").strip().lower()
    if not mime_type:
        mime_type = "image/png"
    if not mime_type.startswith("image/"):
        return None

    sha256 = str(raw.get("sha256") or "").strip().lower()
    return MediaRef(
        kind="image",
        path=path,
        mime_type=mime_type,
        filename=filename,
        sha256=sha256,
    )


def build_user_event_parts(
    message_content: str,
    media_refs: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = [{"text": str(message_content or "")}]

    if not media_refs:
        return parts

    for raw_ref in media_refs:
        normalized = normalize_media_ref(raw_ref)
        if not normalized:
            continue
        parts.append({"media_ref": normalized.to_dict()})

    return parts


def parse_user_event_content(content: Any) -> UserInputPayload:
    text_chunks: List[str] = []
    media_refs: List[MediaRef] = []

    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "text" in part and isinstance(part.get("text"), str):
                    text_chunks.append(part["text"])
                if "media_ref" in part:
                    normalized = normalize_media_ref(part.get("media_ref"))
                    if normalized:
                        media_refs.append(normalized)

        if not text_chunks:
            legacy_content = content.get("content")
            if isinstance(legacy_content, str):
                text_chunks.append(legacy_content)
            elif isinstance(content.get("text"), str):
                text_chunks.append(str(content.get("text") or ""))

    elif isinstance(content, str):
        text_chunks.append(content)

    text = "\n".join(chunk for chunk in text_chunks if isinstance(chunk, str)).strip()
    return UserInputPayload(text=text, media_refs=media_refs)


def payload_to_runner_input(payload: UserInputPayload) -> Tuple[str, List[Dict[str, str]]]:
    media_refs = [ref.to_dict() for ref in payload.media_refs]
    return payload.text, media_refs
