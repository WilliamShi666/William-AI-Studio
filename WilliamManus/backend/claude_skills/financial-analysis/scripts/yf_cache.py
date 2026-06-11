"""
yf_cache.py - 双层缓存：内存 + /tmp JSON 文件，零数据库，零额外依赖。

Layer 1 (内存 dict): 进程生命周期内有效，访问 0 ms
Layer 2 (/tmp JSON): 容器重启前持久，每文件 ~300-500 字节，访问 <5 ms
"""

import json
import os
import time
from typing import Optional

# ── Layer 1: 进程内存 ─────────────────────────────────────────────
_MEM: dict[str, dict] = {}

# ── Layer 2: /tmp 文件 ────────────────────────────────────────────
_TMP_DIR = "/tmp/yfinance_skill_cache"
_TTL_SECONDS = int(os.environ.get("YF_CACHE_TTL_HOURS", "24")) * 3600


def _path(ticker: str) -> str:
    return os.path.join(_TMP_DIR, f"yf_{ticker.upper()}.json")


def get(ticker: str) -> Optional[dict]:
    """
    查缓存。返回 dict（命中且未过期）或 None（未命中/已过期）。
    命中 /tmp 时自动回填内存层，下次访问直接走内存。
    """
    key = ticker.upper()

    # Layer 1: 内存
    if key in _MEM:
        return _MEM[key]

    # Layer 2: /tmp JSON
    path = _path(key)
    if os.path.exists(path):
        try:
            age = time.time() - os.path.getmtime(path)
            if age < _TTL_SECONDS:
                with open(path) as f:
                    data = json.load(f)
                _MEM[key] = data          # 回填内存层
                return data
        except Exception:
            pass                          # 文件损坏则当未命中

    return None


def put(ticker: str, data: dict) -> None:
    """写入内存层和 /tmp 文件层。/tmp 写失败不影响主流程。"""
    key = ticker.upper()
    _MEM[key] = data

    try:
        os.makedirs(_TMP_DIR, exist_ok=True)
        with open(_path(key), "w") as f:
            json.dump(data, f, separators=(",", ":"))   # compact，最小化体积
    except Exception:
        pass


def stats() -> dict:
    """返回缓存状态，供调试用。"""
    files = []
    if os.path.isdir(_TMP_DIR):
        for name in os.listdir(_TMP_DIR):
            p = os.path.join(_TMP_DIR, name)
            files.append({
                "file": name,
                "size_bytes": os.path.getsize(p),
                "age_minutes": round((time.time() - os.path.getmtime(p)) / 60, 1),
            })
    return {
        "memory_entries": len(_MEM),
        "tmp_files": files,
        "ttl_hours": _TTL_SECONDS / 3600,
    }
