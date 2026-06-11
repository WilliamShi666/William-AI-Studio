"""一次性脚本：把 SKILL_AWARENESS_SECTION 上传到 Langfuse 作为 `skill-awareness` prompt。

用法：
    cd WilliamManus/backend
    uv run python scripts/migrate_skill_awareness_to_langfuse.py

幂等性：
    重复执行会在 Langfuse 里创建新 version（v2、v3 ...）。默认每次都会把
    `production` 标签移动到新创建的 version —— 这是 Langfuse 的默认行为。
    如果只想预览不切标签，传 --no-promote。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/migrate_skill_awareness_to_langfuse.py`
# from the backend/ directory without requiring PYTHONPATH=. .
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from agentscope_integration.prompts.skill_awareness import SKILL_AWARENESS_SECTION
from services.langfuse import enabled, langfuse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="不打 production 标签（仅创建新 version，方便先 UI Playground 试）",
    )
    args = parser.parse_args()

    if not enabled:
        print("ERROR: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 未配置", file=sys.stderr)
        return 1

    labels = [] if args.no_promote else ["production"]
    result = langfuse.create_prompt(
        name="skill-awareness",
        type="text",
        prompt=SKILL_AWARENESS_SECTION,
        labels=labels,
    )

    version = getattr(result, "version", "?")
    print(f"OK - skill-awareness pushed; version={version}; labels={labels}")
    print("UI:  http://localhost:3003  →  Prompts  →  skill-awareness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
