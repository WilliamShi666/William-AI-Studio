"""
Claude Agent Service — runs inside PPIO sandbox, wrapping claude_agent_sdk.

This is a lightweight Python script that:
1. Reads JSON request lines from stdin (prompt + config)
2. Creates ClaudeSDKClient with ClaudeAgentOptions
3. Streams Claude Code output as JSON lines to stdout
4. Reports completion with usage/cost

Protocol (stdin/stdout JSON lines):
  Input:  {"prompt": "...", "thread_id": "...", "system_prompt": "...", ...}
  Output: {"type": "stream_event"|"assistant"|"user"|"result", ...}

Usage inside sandbox:
  python claude_agent_service.py < request.json > response.jsonl

Or programmatically:
  from claude_agent_service import run_service
  async for json_line in run_service(stdin_lines):
      print(json_line)
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Iterable, Optional

logger = logging.getLogger(__name__)
_ALLOWED_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions"}
_MAX_DEFAULT_TEAMMATES = 10

try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition
except ImportError:
    ClaudeSDKClient = None  # type: ignore
    ClaudeAgentOptions = None  # type: ignore
    AgentDefinition = None  # type: ignore


def message_to_json(msg: Any) -> str:
    """Convert an SDK message (dict or object) to a JSON line.

    Handles both raw dicts (from stream-json protocol) and SDK typed objects.
    """
    if isinstance(msg, dict):
        return json.dumps(msg, ensure_ascii=False, default=str)
    return json.dumps(_message_to_dict(msg), ensure_ascii=False, default=str)


def _message_to_dict(msg: Any) -> Dict[str, Any]:
    """Convert SDK typed message to a plain dict."""
    msg_type = type(msg).__name__

    if msg_type in ("StreamEvent",):
        payload = {
            "type": "stream_event",
            "event": _event_to_dict(msg.event),
            "parent_tool_use_id": getattr(msg, "parent_tool_use_id", None),
            "uuid": getattr(msg, "uuid", None),
            "session_id": getattr(msg, "session_id", None),
        }
        return {key: value for key, value in payload.items() if value is not None}
    if msg_type == "AssistantMessage":
        content_obj = getattr(msg, "message", None)
        content_blocks = (
            content_obj.content
            if content_obj is not None and hasattr(content_obj, "content")
            else getattr(msg, "content", [])
        )
        return {
            "type": "assistant",
            "message": {
                "id": getattr(content_obj, "id", None) if content_obj is not None else getattr(msg, "message_id", None),
                "content": [_block_to_dict(b) for b in content_blocks],
            },
            **{
                key: value
                for key, value in {
                    "model": getattr(msg, "model", None),
                    "uuid": getattr(msg, "uuid", None),
                    "parent_tool_use_id": getattr(msg, "parent_tool_use_id", None),
                    "session_id": getattr(msg, "session_id", None),
                    "message_id": getattr(msg, "message_id", None),
                    "stop_reason": getattr(msg, "stop_reason", None),
                    "usage": _usage_to_dict(getattr(msg, "usage", None)),
                }.items()
                if value is not None and value != {}
            },
        }
    if msg_type == "UserMessage":
        content_obj = getattr(msg, "message", None)
        content_blocks = (
            content_obj.content
            if content_obj is not None and hasattr(content_obj, "content")
            else getattr(msg, "content", [])
        )
        payload = {
            "type": "user",
            "message": {
                "content": [_block_to_dict(b) for b in content_blocks],
            },
        }
        for key in ("uuid", "parent_tool_use_id", "tool_use_result"):
            value = getattr(msg, key, None)
            if value is not None:
                payload[key] = value
        return payload
    if msg_type == "ResultMessage":
        return {
            "type": "result",
            "subtype": getattr(msg, "subtype", "success"),
            "result": getattr(msg, "result", ""),
            "is_error": getattr(msg, "is_error", False),
            "total_cost_usd": getattr(msg, "total_cost_usd", 0),
            "usage": _usage_to_dict(getattr(msg, "usage", None)),
            "num_turns": getattr(msg, "num_turns", 0),
            "duration_ms": getattr(msg, "duration_ms", 0),
        }
    if msg_type == "SystemMessage":
        return {
            "type": "system",
            "subtype": getattr(msg, "subtype", ""),
            "session_id": getattr(msg, "session_id", ""),
        }

    return {"type": "unknown", "raw": str(msg)}


def _event_to_dict(event: Any) -> Dict[str, Any]:
    if isinstance(event, dict):
        return event
    event_type = type(event).__name__
    d: Dict[str, Any] = {"type": event_type}
    if hasattr(event, "delta"):
        delta = event.delta
        if hasattr(delta, "type"):
            d["delta"] = {"type": delta.type, "text": getattr(delta, "text", "")}
    if hasattr(event, "content_block"):
        d["content_block"] = _block_to_dict(event.content_block)
    return d


def _block_to_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    block_type = getattr(block, "type", None)
    if not block_type:
        class_name = type(block).__name__
        block_type = {
            "TextBlock": "text",
            "ThinkingBlock": "thinking",
            "ToolUseBlock": "tool_use",
            "ToolResultBlock": "tool_result",
            "ServerToolUseBlock": "server_tool_use",
            "ServerToolResultBlock": "tool_result",
        }.get(class_name, "unknown")
    d: Dict[str, Any] = {"type": block_type}
    if block_type == "text":
        d["text"] = getattr(block, "text", "")
    elif block_type == "thinking":
        d["thinking"] = getattr(block, "thinking", "")
    elif block_type == "tool_use":
        d["id"] = getattr(block, "id", "")
        d["name"] = getattr(block, "name", "")
        d["input"] = getattr(block, "input", {})
    elif block_type == "tool_result":
        d["tool_use_id"] = getattr(block, "tool_use_id", "")
        d["content"] = getattr(block, "content", "")
        if getattr(block, "is_error", None) is not None:
            d["is_error"] = getattr(block, "is_error")
    return d


def _usage_to_dict(usage: Any) -> Dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
    }



def _build_default_teammate_agents(
    *,
    system_prompt: str,
    model: str,
    permission_mode: str,
    effort: str,
    mcp_servers: Optional[Dict[str, Any]],
    count: int = 10,
) -> Dict[str, Any]:
    """Build generic same-capability Claude SDK teammate subagents.

    The Claude SDK uses AgentDefinition dataclass instances when available.
    Tests may run without the SDK installed, but this module is imported only
    after the fallback assignment above, so fail loudly if an actual service run
    tries to construct subagents without SDK support.
    """
    if AgentDefinition is None:
        raise RuntimeError("claude_agent_sdk AgentDefinition is not installed")

    base_prompt = system_prompt.strip() or "You are Roys Alpha."
    shared_prompt_suffix = (
        "\n\nYou are a Roys Alpha shadow-clone teammate. "
        "You have the same configuration, tools, skills, and permissions as "
        "the main agent. The main agent will assign your task in the Agent "
        "tool prompt. Do not assume a fixed specialty; adapt to the assigned "
        "work, use the available project context and skills, and return a "
        "concise but complete result for the main agent to integrate."
    )
    server_refs = [mcp_servers] if mcp_servers is not None else None

    bounded_count = max(0, min(count, _MAX_DEFAULT_TEAMMATES))
    agents: Dict[str, Any] = {}
    for index in range(1, bounded_count + 1):
        teammate_name = f"teammate-{index}"
        agents[teammate_name] = AgentDefinition(
            description=(
                f"Generic Roys Alpha teammate {index}. Use when the main "
                "agent wants to delegate any focused subtask to an equal-power "
                "shadow clone."
            ),
            prompt=(
                f"{base_prompt}\n\nYou are teammate {index}."
                f"{shared_prompt_suffix}"
            ),
            tools=None,
            disallowedTools=["Agent", "Task"],
            model=model,
            mcpServers=server_refs,
            effort=effort,
            permissionMode=permission_mode,
        )
    return agents


def build_agent_options(
    *,
    system_prompt: str = "",
    model: str = "deepseek-v4-pro[1m]",
    cwd: str = "/workspace",
    permission_mode: str = "bypassPermissions",
    tools: Optional[Dict[str, Any]] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    effort: str = "max",
    include_partial_messages: bool = True,
    setting_sources: Optional[list[str]] = None,
    skills: str | list[str] | None = "all",
    enable_default_teammates: bool = True,
    teammate_count: int = 10,
) -> Dict[str, Any]:
    """Build ClaudeAgentOptions kwargs dict from configuration.

    Returns a dict suitable for unpacking into ClaudeAgentOptions(**kwargs).
    """
    if permission_mode not in _ALLOWED_PERMISSION_MODES:
        raise ValueError(f"Unsupported permission_mode: {permission_mode}")

    opts: Dict[str, Any] = {
        "model": model,
        "cwd": cwd,
        "permission_mode": permission_mode,
        "include_partial_messages": include_partial_messages,
        "effort": effort,
        "setting_sources": setting_sources or ["project"],
    }
    if system_prompt and system_prompt.strip():
        opts["system_prompt"] = system_prompt
    if skills is not None:
        opts["skills"] = skills

    if tools is not None:
        opts["tools"] = tools
    else:
        opts["tools"] = {"type": "preset", "preset": "claude_code"}

    if mcp_servers is not None:
        opts["mcp_servers"] = mcp_servers

    if env is not None:
        opts["env"] = env

    if enable_default_teammates:
        opts["allowed_tools"] = ["Agent"]
        opts["agents"] = _build_default_teammate_agents(
            system_prompt=system_prompt,
            model=model,
            permission_mode=permission_mode,
            effort=effort,
            mcp_servers=mcp_servers,
            count=teammate_count,
        )

    return opts


async def run_service(
    stdin_lines: Optional[Iterable[str]] = None,
) -> AsyncGenerator[str, None]:
    """Main service loop: read request from stdin, stream JSON lines to stdout.

    Args:
        stdin_lines: Lines of JSON input. Reads from sys.stdin if None.

    Yields:
        JSON strings (one per line) representing SDK messages.
    """
    # Read request
    if stdin_lines is None:
        stdin_lines = sys.stdin

    request_text = ""
    for line in stdin_lines:
        line = line.strip()
        if not line:
            continue
        request_text = line
        break

    if not request_text:
        error_msg = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "No request received on stdin",
        })
        yield error_msg
        return

    try:
        request = json.loads(request_text)
    except json.JSONDecodeError as exc:
        error_msg = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": f"Invalid JSON request: {exc}",
        })
        yield error_msg
        return

    prompt = str(request.get("prompt", "")).strip()
    if not prompt:
        error_msg = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "No prompt provided in request",
        })
        yield error_msg
        return
    if len(prompt) > 100_000:
        error_msg = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "Prompt exceeds maximum length of 100,000 characters",
        })
        yield error_msg
        return

    thread_id = request.get("thread_id", "")
    system_prompt = request.get("system_prompt", "")
    model = request.get("model", "deepseek-v4-pro[1m]")
    cwd = request.get("cwd", "/workspace")
    permission_mode = request.get("permission_mode", "bypassPermissions")
    env_vars = request.get("env", None)
    mcp_servers = request.get("mcp_servers", None)

    try:
        if ClaudeSDKClient is None:
            raise RuntimeError("claude_agent_sdk is not installed")

        opts = ClaudeAgentOptions(**build_agent_options(
            system_prompt=system_prompt,
            model=model,
            cwd=cwd,
            permission_mode=permission_mode,
            env=env_vars,
            mcp_servers=mcp_servers,
        ))

        async with ClaudeSDKClient(options=opts) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                yield message_to_json(msg)
    except Exception as exc:
        logger.error("Service error: %s", exc, exc_info=True)
        error_msg = json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": f"{type(exc).__name__}: {exc}",
        })
        yield error_msg


async def _main() -> None:
    """Entry point when run as a script inside the sandbox."""
    async for line in run_service():
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
