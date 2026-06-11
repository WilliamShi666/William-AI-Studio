"""Resume-state persistence for AgentScope runs.

The coordinator stores lightweight checkpoints in the existing `messages` table
(type=`agent_resume_state`) so interrupted runs can continue from stable
artifacts in `/workspace`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from utils.logger import logger


class ResumeState(BaseModel):
    """Serializable checkpoint for interrupted task recovery."""

    thread_id: str
    project_id: Optional[str] = None
    objective_fingerprint: str
    last_run_id: str
    mode: str = "direct_execution"
    phase: str = "planning"
    completed_steps: List[str] = Field(default_factory=list)
    next_step: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)
    temp_refs: List[str] = Field(default_factory=list)
    summary: str = ""
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class ResumeCoordinator:
    """Read/write resume checkpoints from the shared messages table."""

    RESUME_MESSAGE_TYPE = "agent_resume_state"

    def __init__(self, db_client, thread_id: str, project_id: Optional[str] = None):
        self.db_client = db_client
        self.thread_id = thread_id
        self.project_id = project_id

    @staticmethod
    def normalize_objective(text: str) -> str:
        """Normalize user objective to get stable fingerprints across retries."""
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    @classmethod
    def build_objective_fingerprint(cls, text: str) -> str:
        normalized = cls.normalize_objective(text)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return digest[:24]

    @staticmethod
    def is_resume_intent(user_message: str) -> bool:
        message = (user_message or "").lower()
        patterns = [
            r"\bresume\b",
            r"\bcontinue\b",
            r"继续",
            r"接着",
            r"上次",
            r"刚才",
            r"不要重做",
            r"从中断",
        ]
        return any(re.search(pattern, message) for pattern in patterns)

    @staticmethod
    def _coerce_json(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    async def save_state(
        self,
        state: ResumeState,
        thread_run_id: str,
        resume_version: str = "v1",
    ) -> None:
        """Persist a checkpoint as a row in `messages`."""
        payload = state.model_dump() if hasattr(state, "model_dump") else state.dict()
        metadata = {
            "thread_run_id": thread_run_id,
            "objective_fingerprint": state.objective_fingerprint,
            "resume_version": resume_version,
            "mode": state.mode,
            "phase": state.phase,
        }
        data = {
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "type": self.RESUME_MESSAGE_TYPE,
            "role": "assistant",
            "content": payload,
            "is_llm_message": False,
            "metadata": metadata,
        }
        await self.db_client.table("messages").insert(data)

    async def load_latest_state(
        self,
        objective_fingerprint: Optional[str],
        *,
        resume_window_minutes: int = 1440,
        exclude_run_id: Optional[str] = None,
        allow_latest_without_fingerprint: bool = False,
    ) -> Optional[ResumeState]:
        """Load the most recent checkpoint for this thread.

        When `allow_latest_without_fingerprint=True`, the coordinator falls back to
        the latest checkpoint regardless of objective fingerprint (used for
        explicit "continue" intents).
        """

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=resume_window_minutes)

        async def _query_rows(filter_fingerprint: Optional[str]):
            query = (
                self.db_client.table("messages")
                .select("content,metadata,created_at")
                .eq("thread_id", self.thread_id)
                .eq("type", self.RESUME_MESSAGE_TYPE)
                .order("created_at", desc=True)
                .limit(50)
                # PostgreSQL query builder expects native datetime for timestamptz columns.
                .gte("created_at", cutoff)
            )
            if filter_fingerprint:
                query = query.contains(
                    "metadata",
                    {"objective_fingerprint": filter_fingerprint},
                )
            result = await query.execute()
            return result.data or []

        rows = await _query_rows(objective_fingerprint)
        if not rows and allow_latest_without_fingerprint:
            rows = await _query_rows(None)

        for row in rows:
            metadata = self._coerce_json(row.get("metadata", {}))
            if exclude_run_id and metadata.get("thread_run_id") == exclude_run_id:
                continue

            content = self._coerce_json(row.get("content", {}))
            if not content:
                continue

            # Guard against stale rows accidentally matching due backend json quirks.
            if objective_fingerprint and not allow_latest_without_fingerprint:
                fp = content.get("objective_fingerprint") or metadata.get(
                    "objective_fingerprint"
                )
                if fp != objective_fingerprint:
                    continue

            try:
                if hasattr(ResumeState, "model_validate"):
                    return ResumeState.model_validate(content)
                return ResumeState.parse_obj(content)
            except Exception as exc:
                logger.warning("[ResumeCoordinator] Invalid resume row skipped: %s", exc)
                continue

        return None

    @staticmethod
    def build_resume_hint(state: ResumeState) -> str:
        """Create a compact prompt block used to continue from checkpoints."""
        completed = "\n".join(f"- {item}" for item in state.completed_steps) or "- (none)"
        artifacts = "\n".join(
            f"- {key}: /workspace/{value}" if not value.startswith("/workspace/") else f"- {key}: {value}"
            for key, value in (state.artifacts or {}).items()
            if value
        ) or "- (none)"

        summary = (state.summary or "").strip()
        if len(summary) > 1200:
            summary = summary[:1200] + "\n...[truncated]"

        next_step = state.next_step or "(not specified)"

        return (
            "## Resume Context\n"
            "An earlier run for this thread exists. Continue from current progress, "
            "reuse existing artifacts, and avoid restarting from scratch.\n\n"
            f"- Last phase: {state.phase}\n"
            f"- Next step: {next_step}\n"
            f"- Updated at: {state.updated_at}\n\n"
            "### Completed Steps\n"
            f"{completed}\n\n"
            "### Reusable Artifacts\n"
            f"{artifacts}\n\n"
            "### Snapshot Summary\n"
            f"{summary or '(no summary)'}"
        )
