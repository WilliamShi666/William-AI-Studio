"""一次性脚本：把 orchestrator-system + worker-system 上传到 Langfuse。

上传时把 `__SKILL_AWARENESS_SECTION__` 占位符替换为 Langfuse composability 标签,
让 Langfuse 内部负责解析 skill-awareness 引用 (等价于 Phase 1 已迁的独立 prompt)。

用法：
    cd WilliamManus/backend
    uv run python scripts/migrate_orch_worker_to_langfuse.py
    # 或只上传一个:
    uv run python scripts/migrate_orch_worker_to_langfuse.py --only orchestrator
    uv run python scripts/migrate_orch_worker_to_langfuse.py --only worker

幂等：每次跑生成新 version + 把 production 标签移到新 version (Langfuse 默认行为)。
传 --no-promote 只创建版本不切标签。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from agentscope_integration.prompts.orchestrator_prompt import ORCHESTRATOR_PROMPT_RAW_TEMPLATE
from agentscope_integration.prompts.worker_prompt import WORKER_PROMPT_RAW_TEMPLATE
from services.langfuse import enabled, langfuse

COMPOSABILITY_TAG = "@@@langfusePrompt:name=skill-awareness|label=production@@@"


def _to_langfuse_body(raw_template: str) -> str:
    """Replace local placeholder with Langfuse composability reference."""
    return raw_template.replace("__SKILL_AWARENESS_SECTION__", COMPOSABILITY_TAG)


def _upload(name: str, body: str, *, promote: bool) -> None:
    labels = ["production"] if promote else []
    result = langfuse.create_prompt(
        name=name,
        type="text",
        prompt=body,
        labels=labels,
    )
    version = getattr(result, "version", "?")
    print(f"OK - {name} pushed; version={version}; labels={labels}; chars={len(body)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=["orchestrator", "worker"],
        help="Only upload one of the two prompts",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        help="Skip assigning the `production` label",
    )
    args = parser.parse_args()

    if not enabled:
        print("ERROR: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not configured", file=sys.stderr)
        return 1

    promote = not args.no_promote

    targets = {
        "orchestrator": ("orchestrator-system", ORCHESTRATOR_PROMPT_RAW_TEMPLATE),
        "worker": ("worker-system", WORKER_PROMPT_RAW_TEMPLATE),
    }

    if args.only:
        name, raw = targets[args.only]
        _upload(name, _to_langfuse_body(raw), promote=promote)
    else:
        for name, raw in targets.values():
            _upload(name, _to_langfuse_body(raw), promote=promote)

    print("UI:  http://localhost:3003  →  Prompts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
