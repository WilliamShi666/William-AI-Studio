"""FastAPI service wrapper for the multi-agent debate workflow.

This module exposes a small HTTP API around the AgentScope-based
``run_multiagent_debate`` workflow so that the frontend can trigger
debates and retrieve structured results.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Callable

import jwt
from fastapi import FastAPI, HTTPException, Response, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agentscope.message import Msg
from agentscope.pipeline import MsgHub

from .main import (
    TOTAL_ROUNDS,
    ROUND_LABELS,
    JudgeModel,
    build_debater_prompt,
    create_solver_agent,
    create_moderator_agent,
)

UNSAFE_JWT_SECRETS = {
    "-".join(parts)
    for parts in (
        ("your", "secret", "key", "change", "in", "production"),
        ("your", "secret", "key", "change", "this", "in", "production"),
        ("your", "super", "secret", "jwt", "key", "change", "this", "in", "production"),
    )
}
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


def _is_public_path(path: str) -> bool:
  return path in PUBLIC_PATHS or path.startswith("/docs")


def _require_jwt_secret() -> str:
  if not JWT_SECRET_KEY or JWT_SECRET_KEY in UNSAFE_JWT_SECRETS or len(JWT_SECRET_KEY) < 32:
    raise HTTPException(status_code=500, detail="JWT_SECRET_KEY is not configured safely")
  return JWT_SECRET_KEY


async def verify_jwt(request: Request) -> str:
  if request.method == "OPTIONS" or _is_public_path(request.url.path):
    return "anonymous"

  auth_header = request.headers.get("Authorization", "")
  token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None
  if not token:
    token = request.query_params.get("token")
  if not token:
    raise HTTPException(
        status_code=401,
        detail="No valid authentication credentials found",
        headers={"WWW-Authenticate": "Bearer"},
    )

  try:
    payload = jwt.decode(token, _require_jwt_secret(), algorithms=[JWT_ALGORITHM])
  except jwt.ExpiredSignatureError:
    raise HTTPException(
        status_code=401,
        detail="Token expired",
        headers={"WWW-Authenticate": "Bearer"},
    )
  except jwt.InvalidTokenError:
    raise HTTPException(
        status_code=401,
        detail="Invalid token",
        headers={"WWW-Authenticate": "Bearer"},
    )

  user_id = payload.get("sub")
  if not user_id:
    raise HTTPException(
        status_code=401,
        detail="Invalid token",
        headers={"WWW-Authenticate": "Bearer"},
    )
  return user_id


class HealthResponse(BaseModel):
  """Simple health check response."""

  status: str = "ok"
  service: str = "multiagent_debaters"
  version: str = "1.0.0"


class DebateRunRequest(BaseModel):
  """Request body for running a debate."""

  topic: str = Field(..., description="Debate topic / motion")
  model_name: str | None = Field(
      default=None,
      description=(
          "Optional model name for the debater and moderator agents. "
          "Defaults to 'deepseek-v4-flash' if omitted."
      ),
  )


class DebateJudgeDecision(BaseModel):
  """Moderator decision for a specific round."""

  finished: bool = Field(
      ...,
      description=(
          "Whether the moderator decides the debate can stop after this round."
      ),
  )
  notes: str | None = Field(
      default=None,
      description=(
          "Optional evaluation summary from the moderator about the debate so far."
      ),
  )
  suggested_answer: str | None = Field(
      default=None,
      description=(
          "Optional synthesized viewpoint based on the debate so far. "
          "This is a reference perspective, not a unique correct answer."
      ),
  )


class DebateRound(BaseModel):
  """One debate round's transcript."""

  round_id: int
  label: str
  affirmative_speech: str
  opposition_speech: str
  judge: DebateJudgeDecision


class DebateRunResponse(BaseModel):
  """Structured response for a full debate run (non-streaming)."""

  topic: str
  max_rounds: int
  total_rounds_run: int
  finished_early: bool
  model_name: str
  rounds: list[DebateRound]


class ErrorResponse(BaseModel):
  """Standard error response shape."""

  code: str
  message: str
  details: dict[str, Any] | None = None


app = FastAPI(
    title="Multi-Agent Debate Service",
    version="1.0.0",
    description=(
        "HTTP API wrapper for the AgentScope-based multi-agent debate workflow."
    ),
    dependencies=[Depends(verify_jwt)],
)

# Allow cross-origin requests so the Vite frontend (different port) can call this API.
_ALLOWED_ORIGINS = [
    "*",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _extract_text_from_msg(msg: Msg) -> str:
  """Best-effort extraction of plain text content from an AgentScope Msg."""
  content = getattr(msg, "content", "")
  if isinstance(content, str):
    return content
  # Try to use content blocks if available.
  try:
    blocks = msg.get_content_blocks()  # type: ignore[attr-defined]
    parts: list[str] = []
    for block in blocks:
      if isinstance(block, dict):
        btype = block.get("type")
        if btype == "text":
          parts.append(str(block.get("text", "")))
      else:
        btype = getattr(block, "type", None)
        if btype == "text":
          parts.append(str(getattr(block, "text", "")))
    if parts:
      return "".join(parts)
  except Exception:  # noqa: BLE001
    pass
  # Fallback: convert structured content to string for debugging / display.
  try:
    return str(content)
  except Exception:  # noqa: BLE001
    return ""


def _resolve_judge_evaluation(msg: Msg, metadata: dict[str, Any]) -> str | None:
  """Resolve evaluation text from structured metadata or fall back to content."""
  evaluation = metadata.get("evaluation")
  if evaluation is not None and not isinstance(evaluation, str):
    evaluation = str(evaluation)
  if isinstance(evaluation, str) and evaluation.strip():
    return evaluation.strip()

  fallback = _extract_text_from_msg(msg).strip()
  if not fallback:
    return None

  evaluation_text, _ = _split_judge_output(fallback)
  return evaluation_text or fallback


def _split_judge_output(text: str) -> tuple[str | None, str | None]:
  """Split a moderator output into evaluation and final judgment if possible."""
  trimmed = text.strip()
  if not trimmed:
    return None, None

  markers = ["最终裁决", "Final Judgment", "Final Answer", "最终答案", "正确答案"]
  marker = next((m for m in markers if m in trimmed), None)
  if marker is None:
    return trimmed, None

  idx = trimmed.rfind(marker)
  evaluation = trimmed[:idx].strip()
  evaluation = evaluation.lstrip("综合评议:：- ").strip()
  judgement = trimmed[idx + len(marker) :].lstrip(":：- \n").strip()
  if judgement in {"尚未裁决", "未裁决", "TBD", "N/A"}:
    judgement = ""

  return (evaluation or None, judgement or None)


def _make_stream_pre_print_hook(
    side: str,
    current_round_ref: dict[str, int],
    queue: "asyncio.Queue[dict[str, Any]]",
) -> Callable[[Any, dict[str, Any]], dict[str, Any] | None]:
  """Create a pre_print hook that forwards incremental content to a queue.

  This hook is attached to individual ReActAgent instances so that every time
  the agent prints a message during streaming, we can forward the latest
  accumulated content to the HTTP streaming endpoint.
  """

  def _hook(self: Any, kwargs: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ARG001
    msg = kwargs.get("msg")
    if msg is None:
      return kwargs

    # Try to use content blocks if available to extract text-only content.
    full_text = ""
    try:
      # get_content_blocks is available on Msg in AgentScope
      blocks = msg.get_content_blocks()  # type: ignore[attr-defined]
      parts: list[str] = []
      for block in blocks:
        # Blocks can be dict-like or objects; support both.
        if isinstance(block, dict):
          btype = block.get("type")
          if btype == "text":
            parts.append(str(block.get("text", "")))
        else:
          btype = getattr(block, "type", None)
          if btype == "text":
            parts.append(str(getattr(block, "text", "")))
      full_text = "".join(parts)
    except Exception:  # noqa: BLE001
      # Fallback to raw content representation
      full_text = str(getattr(msg, "content", ""))

    if not full_text:
      return kwargs

    event = {
        "type": "content",
        "side": side,
        "round_id": current_round_ref["value"],
        "full": full_text,
    }

    try:
      queue.put_nowait(event)
    except Exception:  # noqa: BLE001
      # Best-effort: if queue is full or closed, ignore streaming for this chunk.
      pass

    return kwargs

  return _hook


async def run_multiagent_debate_structured(
    topic: str,
    model_name: str,
) -> DebateRunResponse:
  """Run the debate workflow and return a structured transcript.

  This mirrors the logic in ``run_multiagent_debate`` but captures each
  debater's response and the moderator's decision for every round.
  """
  # Create agents with the requested model
  affirmative = create_solver_agent("Affirmative", topic, model_name=model_name)
  opposition = create_solver_agent("Opposition", topic, model_name=model_name)
  moderator = create_moderator_agent(topic, model_name=model_name)

  rounds: list[DebateRound] = []
  current_round = 1
  finished_early = False

  while current_round <= TOTAL_ROUNDS:
    # Broadcast messages within a hub so agents can share context
    async with MsgHub(participants=[affirmative, opposition, moderator]):
      # Affirmative always speaks first
      aff_msg = await affirmative(
          Msg(
              "user",
              build_debater_prompt("affirmative", current_round, topic),
              "user",
          ),
      )

      # Opposition responds second
      opp_msg = await opposition(
          Msg(
              "user",
              build_debater_prompt("opposition", current_round, topic),
              "user",
          ),
      )

    # Moderator decides whether to continue
    msg_judge = await moderator(
        Msg(
            "user",
            (
                "You have just observed Round "
                f"{current_round} of a structured three-round debate. "
                "Based on the full debate so far, provide an evaluation and "
                "decide whether the debate is finished. "
                "If this is not Round 3, set `finished = false` and "
                "`correct_answer = null`. "
                "Only in Round 3 should you set `finished = true` and "
                "provide the final `correct_answer`. "
                "Also respond in Chinese with the following format:\n"
                "综合评议：<your evaluation>\n"
                "最终裁决：<final answer, or 尚未裁决 if not Round 3>."
            ),
            "user",
        ),
        structured_model=JudgeModel,
    )

    metadata = getattr(msg_judge, "metadata", None) or {}
    finished = bool(metadata.get("finished"))
    evaluation = _resolve_judge_evaluation(msg_judge, metadata)
    suggested_answer = metadata.get("correct_answer")
    if not suggested_answer:
      _, parsed_answer = _split_judge_output(_extract_text_from_msg(msg_judge))
      suggested_answer = parsed_answer

    if current_round < TOTAL_ROUNDS:
      if finished:
        finished_early = True
      finished = False
      suggested_answer = None
    elif not finished and suggested_answer:
      finished = True

    if suggested_answer is not None and not isinstance(suggested_answer, str):
      suggested_answer = str(suggested_answer)

    round_obj = DebateRound(
        round_id=current_round,
        label=ROUND_LABELS[current_round],
        affirmative_speech=_extract_text_from_msg(aff_msg),
        opposition_speech=_extract_text_from_msg(opp_msg),
        judge=DebateJudgeDecision(
            finished=finished,
            notes=evaluation,
            suggested_answer=suggested_answer,
        ),
    )
    rounds.append(round_obj)

    current_round += 1

  return DebateRunResponse(
      topic=topic,
      max_rounds=TOTAL_ROUNDS,
      total_rounds_run=len(rounds),
      finished_early=finished_early,
      model_name=model_name,
      rounds=rounds,
  )


async def run_multiagent_debate_stream(
    topic: str,
    model_name: str,
    queue: "asyncio.Queue[dict[str, Any]]",
) -> None:
  """Run the debate workflow and stream incremental content via a queue.

  This mirrors the high-level structure of ``run_multiagent_debate`` but uses
  per-agent ``pre_print`` hooks to forward the latest accumulated content to
  the provided queue so that an HTTP streaming endpoint can forward it to the
  frontend in real time.
  """
  affirmative = create_solver_agent("Affirmative", topic, model_name=model_name)
  opposition = create_solver_agent("Opposition", topic, model_name=model_name)
  moderator = create_moderator_agent(topic, model_name=model_name)

  current_round_ref: dict[str, int] = {"value": 1}

  # Attach instance-level hooks so we only affect these agents in this request.
  affirmative.register_instance_hook(
      "pre_print",
      "debate_stream_pre_print_affirmative",
      _make_stream_pre_print_hook("affirmative", current_round_ref, queue),
  )
  opposition.register_instance_hook(
      "pre_print",
      "debate_stream_pre_print_opposition",
      _make_stream_pre_print_hook("opposition", current_round_ref, queue),
  )
  moderator.register_instance_hook(
      "pre_print",
      "debate_stream_pre_print_moderator",
      _make_stream_pre_print_hook("moderator", current_round_ref, queue),
  )

  current_round = 1

  while current_round <= TOTAL_ROUNDS:
    current_round_ref["value"] = current_round

    async with MsgHub(participants=[affirmative, opposition, moderator]):
      # Affirmative always speaks first
      await affirmative(
          Msg(
              "user",
              build_debater_prompt("affirmative", current_round, topic),
              "user",
          ),
      )

      # Opposition responds second
      await opposition(
          Msg(
              "user",
              build_debater_prompt("opposition", current_round, topic),
              "user",
          ),
      )

    # Moderator decides whether to continue (we keep the same logic as the
    # non-streaming version but do not currently stream moderator content).
    msg_judge = await moderator(
        Msg(
            "user",
            (
                "You have just observed Round "
                f"{current_round} of a structured three-round debate. "
                "Based on the full debate so far, provide an evaluation and "
                "decide whether the debate is finished. "
                "If this is not Round 3, set `finished = false` and "
                "`correct_answer = null`. "
                "Only in Round 3 should you set `finished = true` and "
                "provide the final `correct_answer`. "
                "Also respond in Chinese with the following format:\n"
                "综合评议：<your evaluation>\n"
                "最终裁决：<final answer, or 尚未裁决 if not Round 3>."
            ),
            "user",
        ),
        structured_model=JudgeModel,
    )

    metadata = getattr(msg_judge, "metadata", None) or {}
    finished = bool(metadata.get("finished"))
    evaluation = _resolve_judge_evaluation(msg_judge, metadata)
    suggested_answer = metadata.get("correct_answer")
    if not suggested_answer:
      _, parsed_answer = _split_judge_output(_extract_text_from_msg(msg_judge))
      suggested_answer = parsed_answer

    if current_round < TOTAL_ROUNDS:
      finished = False
      suggested_answer = None
    elif not finished and suggested_answer:
      finished = True

    if suggested_answer is not None and not isinstance(suggested_answer, str):
      suggested_answer = str(suggested_answer)

    await queue.put(
        {
            "type": "judge",
            "round_id": current_round,
            "decision": {
                "finished": finished,
                "notes": evaluation,
                "suggested_answer": suggested_answer,
            },
        },
    )

    current_round += 1

  # Signal end of streaming to the HTTP generator
  await queue.put({"type": "end", "total_rounds": TOTAL_ROUNDS})


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
  """Simple health endpoint for monitoring and orchestration."""
  return HealthResponse()


@app.options("/debates/run")
async def options_debate() -> Response:
  """Handle CORS preflight for the debate endpoint."""
  return Response(
      status_code=204,
      headers={
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
      },
  )


@app.post(
    "/debates/run",
    response_model=DebateRunResponse,
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def run_debate(
    request: DebateRunRequest,
    response: Response,
) -> DebateRunResponse:
  """HTTP endpoint to trigger a multi-agent debate."""
  topic = request.topic.strip()
  if not topic:
    raise HTTPException(
        status_code=400,
        detail=ErrorResponse(
            code="INVALID_REQUEST",
            message="Topic must not be empty.",
        ).model_dump(),
    )

  model_name = request.model_name.strip() if request.model_name else "deepseek-v4-flash"

  # Ensure CORS header is present on the main response as well
  response.headers["Access-Control-Allow-Origin"] = "*"

  try:
    return await run_multiagent_debate_structured(topic=topic, model_name=model_name)
  except HTTPException:
    # Re-raise FastAPI HTTP exceptions directly
    raise
  except Exception as exc:  # noqa: BLE001
    # For now, treat unexpected failures as 500 errors.
    # If the underlying LLM provider exposes quota / rate limit errors, map
    # them to 429 instead.
    raise HTTPException(
        status_code=500,
        detail=ErrorResponse(
            code="INTERNAL_ERROR",
            message="Failed to run multi-agent debate.",
            details={"error": str(exc)},
        ).model_dump(),
    ) from exc


@app.post(
    "/debates/stream",
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def stream_debate(
    request: DebateRunRequest,
) -> StreamingResponse:
  """HTTP streaming endpoint to trigger a multi-agent debate.

  This endpoint keeps using ReActAgent internally but relies on per-instance
  ``pre_print`` hooks to forward incremental content to the frontend.
  """
  topic = request.topic.strip()
  if not topic:
    raise HTTPException(
        status_code=400,
        detail=ErrorResponse(
            code="INVALID_REQUEST",
            message="Topic must not be empty.",
        ).model_dump(),
    )

  model_name = request.model_name.strip() if request.model_name else "deepseek-v4-flash"

  queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()

  async def event_generator() -> AsyncIterator[bytes]:
    debate_task = asyncio.create_task(
        run_multiagent_debate_stream(
            topic=topic,
            model_name=model_name,
            queue=queue,
        ),
    )
    try:
      while True:
        event = await queue.get()
        chunk = json.dumps(event, ensure_ascii=False) + "\n"
        yield chunk.encode("utf-8")
        if event.get("type") == "end":
          break
    finally:
      if not debate_task.done():
        debate_task.cancel()

  return StreamingResponse(
      event_generator(),
      media_type="application/json",
      headers={
          "Access-Control-Allow-Origin": "*",
      },
  )
