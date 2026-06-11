import datetime
import os
import re
import shlex
from typing import Dict, Optional, Tuple

from agentscope.message import Msg

from utils.logger import logger

WORKSPACE_PREFIX = "/workspace/"
LOCAL_PATH_PREFIXES = (
    "/tmp/",
    "/home/",
    "/root/",
    "/var/",
    "/opt/",
    "/mnt/",
    "/data/",
    "/Users/",
    "/private/",
    "/etc/",
)

_PATH_PATTERN = re.compile(r"(?:file://)?(/[^\\s\\)\\]\\}>\"'`]+)")


def _extract_candidate_paths(text: str) -> list[str]:
    seen = set()
    candidates: list[str] = []
    for match in _PATH_PATTERN.finditer(text):
        raw_path = match.group(1)
        cleaned = raw_path.rstrip(".,;:)]}>\"'`")
        if not cleaned or cleaned.startswith(WORKSPACE_PREFIX):
            continue
        if not cleaned.startswith(LOCAL_PATH_PREFIXES):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        candidates.append(cleaned)
    return candidates


def _extract_tool_func(toolkit, tool_name: str):
    if not toolkit or not hasattr(toolkit, "tools"):
        return None
    tool = toolkit.tools.get(tool_name)
    if not tool:
        return None
    return getattr(tool, "original_func", None) or getattr(tool, "func", None)


async def _run_command(exec_func, command: str) -> Tuple[bool, str]:
    result = exec_func(command=command, workdir="")
    if hasattr(result, "__await__"):
        result = await result
    success = getattr(result, "success", True)
    output = getattr(result, "output", "")
    if isinstance(output, dict):
        text = (
            output.get("stdout")
            or output.get("output")
            or output.get("stderr")
            or ""
        )
    else:
        text = str(output)
    return bool(success), text


async def _path_exists(exec_func, path: str) -> bool:
    _, output = await _run_command(
        exec_func,
        f"test -e {shlex.quote(path)} && echo __exists__",
    )
    return "__exists__" in output


async def _ensure_workspace_copy(
    exec_func,
    source_path: str,
) -> Optional[str]:
    base_name = os.path.basename(source_path.rstrip("/"))
    if not base_name:
        return None

    destination = f"{WORKSPACE_PREFIX}{base_name}"
    if await _path_exists(exec_func, destination):
        name_root, ext = os.path.splitext(base_name)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = f"{WORKSPACE_PREFIX}{name_root}-{timestamp}{ext}"

    command = (
        f"if [ -f {shlex.quote(source_path)} ]; then "
        f"cp -f {shlex.quote(source_path)} {shlex.quote(destination)}; "
        f"elif [ -d {shlex.quote(source_path)} ]; then "
        f"cp -R {shlex.quote(source_path)} {shlex.quote(destination)}; "
        f"fi"
    )
    await _run_command(exec_func, command)
    if await _path_exists(exec_func, destination):
        return destination
    return None


def _update_msg_text(msg: Msg, updated_text: str) -> None:
    if isinstance(msg.content, str):
        msg.content = updated_text
        return
    blocks = msg.get_content_blocks()
    for block in blocks:
        if block.get("type") == "text":
            block["text"] = updated_text
    msg.content = blocks


async def relocate_external_paths(
    msg: Msg,
    toolkit,
    *,
    label: str = "agent",
) -> Tuple[Msg, Dict[str, str]]:
    """Move non-/workspace paths mentioned in msg into /workspace, update msg text."""
    text = msg.get_text_content() if hasattr(msg, "get_text_content") else None
    if not text:
        return msg, {}

    candidates = _extract_candidate_paths(text)
    if not candidates:
        return msg, {}

    exec_func = _extract_tool_func(toolkit, "execute_command")
    if not exec_func:
        logger.warning("[%s] workspace guard: execute_command not available", label)
        return msg, {}

    replacements: Dict[str, str] = {}
    for path in candidates:
        try:
            destination = await _ensure_workspace_copy(exec_func, path)
            if destination:
                replacements[path] = destination
        except Exception as exc:
            logger.warning(
                "[%s] workspace guard failed for %s: %s",
                label,
                path,
                exc,
            )

    if not replacements:
        return msg, {}

    updated_text = text
    for source, destination in replacements.items():
        updated_text = updated_text.replace(source, destination)

    _update_msg_text(msg, updated_text)
    return msg, replacements
