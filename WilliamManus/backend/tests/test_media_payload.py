from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent.media_payload import (  # noqa: E402
    build_user_event_parts,
    normalize_media_ref,
    parse_user_event_content,
)


def test_build_user_event_parts_includes_image_refs() -> None:
    parts = build_user_event_parts(
        "Analyze this image",
        media_refs=[
            {
                "kind": "image",
                "path": "/workspace/demo.png",
                "mime_type": "image/png",
                "filename": "demo.png",
                "sha256": "abc123",
            }
        ],
    )

    assert parts[0] == {"text": "Analyze this image"}
    assert parts[1]["media_ref"]["path"] == "/workspace/demo.png"
    assert parts[1]["media_ref"]["kind"] == "image"


def test_normalize_media_ref_rejects_invalid_payloads() -> None:
    assert normalize_media_ref({"kind": "video", "path": "/workspace/a.mp4"}) is None
    assert (
        normalize_media_ref(
            {
                "kind": "image",
                "path": "/tmp/a.png",
                "mime_type": "image/png",
                "filename": "a.png",
            }
        )
        is None
    )


def test_parse_user_event_content_handles_parts_and_legacy_text() -> None:
    payload = parse_user_event_content(
        {
            "role": "user",
            "parts": [
                {"text": "line1"},
                {
                    "media_ref": {
                        "kind": "image",
                        "path": "/workspace/x.png",
                        "mime_type": "image/png",
                        "filename": "x.png",
                    }
                },
                {"text": "line2"},
            ],
        }
    )
    assert payload.text == "line1\nline2"
    assert len(payload.media_refs) == 1
    assert payload.media_refs[0].path == "/workspace/x.png"

    legacy = parse_user_event_content({"content": "legacy message"})
    assert legacy.text == "legacy message"
    assert legacy.media_refs == []
