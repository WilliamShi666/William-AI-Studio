import asyncio
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from services.postgresql import DBConnection
from utils.config import config
from utils.logger import logger


WORKSPACE_ROOT = "/workspace"
_HIDDEN_WORKSPACE_PREFIXES = (
    "/workspace/skills",
    "/workspace/.claude",
    "/workspace/.git",
    "/workspace/node_modules",
    "/workspace/__pycache__",
    "/workspace/.pytest_cache",
    "/workspace/.mypy_cache",
    "/workspace/.ruff_cache",
)
_HIDDEN_WORKSPACE_FILES = {
    "/workspace/claude_skills_sandbox_bootstrap.py",
    "/workspace/_temp_skill_code.py",
    "/workspace/.env",
}
_SAFE_KEY_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_CLAUDE_LOCAL_SANDBOX_PREFIX = "claude-local:"


def _maybe_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_decoded_path(path: str) -> str:
    candidate = urllib.parse.unquote(str(path or "").strip())
    if not candidate:
        raise ValueError("Workspace artifact path cannot be empty")
    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    normalized = posixpath.normpath(candidate)
    if normalized == ".":
        normalized = WORKSPACE_ROOT
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def normalize_workspace_artifact_path(path: str, *, allow_root: bool = True) -> str:
    normalized = _normalize_decoded_path(path)
    if normalized != WORKSPACE_ROOT and not normalized.startswith(f"{WORKSPACE_ROOT}/"):
        raise ValueError(f"Workspace artifact path must stay under {WORKSPACE_ROOT}: {path}")
    if not allow_root and normalized == WORKSPACE_ROOT:
        raise ValueError("Workspace artifact path must point to a file under /workspace")
    return normalized


def is_hidden_workspace_artifact_path(path: str) -> bool:
    normalized = _normalize_decoded_path(path)
    if normalized in _HIDDEN_WORKSPACE_FILES:
        return True
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _HIDDEN_WORKSPACE_PREFIXES
    )


def is_user_visible_workspace_artifact_path(path: str) -> bool:
    try:
        normalized = normalize_workspace_artifact_path(path)
    except ValueError:
        return False
    return not is_hidden_workspace_artifact_path(normalized)


def _guess_content_type(path: str, content: Any, explicit_content_type: Optional[str]) -> str:
    if explicit_content_type:
        return str(explicit_content_type)
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if isinstance(content, str):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def guess_workspace_artifact_content_type(
    path: str,
    content: Any = None,
    explicit_content_type: Optional[str] = None,
) -> str:
    return _guess_content_type(path, content, explicit_content_type)


def _coerce_content_bytes(content: Any) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    return str(content).encode("utf-8")


def _storage_key_component(value: str, *, fallback: str) -> str:
    candidate = _SAFE_KEY_SEGMENT.sub("-", value).strip(".-")
    return candidate or fallback


def _parse_claude_local_agent_run_id(*sandbox_ids: str) -> Optional[str]:
    for sandbox_id in sandbox_ids:
        normalized = str(sandbox_id or "").strip()
        if normalized.startswith(_CLAUDE_LOCAL_SANDBOX_PREFIX):
            run_id = normalized[len(_CLAUDE_LOCAL_SANDBOX_PREFIX):].strip()
            if run_id:
                return run_id
    return None


def _default_local_root() -> str:
    return str(Path(__file__).resolve().parents[1] / ".workspace_artifacts")


@dataclass
class WorkspaceArtifactRecord:
    artifact_id: str
    project_id: str
    path: str
    storage_backend: str
    storage_key: str
    content_type: Optional[str]
    size_bytes: int
    sha256: str
    source: str
    sandbox_id: Optional[str]
    thread_id: Optional[str]
    agent_run_id: Optional[str]
    created_by_user_id: Optional[str]
    metadata: Dict[str, Any]
    created_at: Optional[str]
    updated_at: Optional[str]


_SANDBOX_HINT_SCALAR_METADATA_KEYS = (
    "sandbox_id",
    "requested_sandbox_id",
    "resolved_sandbox_id",
    "project_sandbox_id",
)
_SANDBOX_HINT_ARRAY_METADATA_KEYS = (
    "sandbox_id_aliases",
    "sandbox_ids",
)


def _iter_sandbox_hint_values(value: Any) -> Iterable[str]:
    if isinstance(value, (list, tuple, set)):
        for item in value:
            normalized = str(item or "").strip()
            if normalized:
                yield normalized
        return

    normalized = str(value or "").strip()
    if normalized:
        yield normalized


def _build_sandbox_hint_aliases(
    *,
    sandbox_id: Optional[str],
    metadata: Optional[Dict[str, Any]],
    existing_record: Optional[WorkspaceArtifactRecord],
) -> List[str]:
    aliases: List[str] = []
    seen_aliases: set[str] = set()

    def _add_alias(value: Any) -> None:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen_aliases:
            return
        seen_aliases.add(normalized)
        aliases.append(normalized)

    if existing_record is not None:
        _add_alias(existing_record.sandbox_id)
        existing_metadata = existing_record.metadata
    else:
        existing_metadata = {}

    for metadata_source in (existing_metadata, metadata or {}):
        for key in _SANDBOX_HINT_SCALAR_METADATA_KEYS:
            _add_alias(metadata_source.get(key))
        for key in _SANDBOX_HINT_ARRAY_METADATA_KEYS:
            for alias_value in _iter_sandbox_hint_values(metadata_source.get(key)):
                _add_alias(alias_value)

    _add_alias(sandbox_id)
    return aliases


class LocalArtifactBlobBackend:
    name = "local"

    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)

    def _resolve_path(self, storage_key: str) -> str:
        storage_path = os.path.abspath(os.path.join(self.root_dir, storage_key))
        root_prefix = f"{self.root_dir}{os.sep}"
        if storage_path != self.root_dir and not storage_path.startswith(root_prefix):
            raise ValueError(f"Unsafe workspace artifact key: {storage_key}")
        return storage_path

    async def write_bytes(self, storage_key: str, content: bytes) -> None:
        path = self._resolve_path(storage_key)

        def _write() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content)

        await asyncio.to_thread(_write)

    async def read_bytes(self, storage_key: str) -> bytes:
        path = self._resolve_path(storage_key)

        def _read() -> bytes:
            with open(path, "rb") as handle:
                return handle.read()

        return await asyncio.to_thread(_read)

    async def exists_bytes(self, storage_key: str) -> bool:
        path = self._resolve_path(storage_key)
        return await asyncio.to_thread(os.path.isfile, path)

    async def delete_bytes(self, storage_key: str) -> None:
        path = self._resolve_path(storage_key)

        def _delete() -> None:
            try:
                os.remove(path)
            except FileNotFoundError:
                return

        await asyncio.to_thread(_delete)


class AliyunOSSArtifactBlobBackend:
    name = "aliyun_oss"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket_name: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        self.endpoint = endpoint
        self.bucket_name = bucket_name
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret

    def _build_bucket(self):
        try:
            import oss2  # type: ignore
        except ImportError as import_error:
            raise RuntimeError(
                "Aliyun OSS artifact backend requires the optional 'oss2' dependency"
            ) from import_error

        auth = oss2.Auth(self.access_key_id, self.access_key_secret)
        return oss2.Bucket(auth, self.endpoint, self.bucket_name)

    async def write_bytes(self, storage_key: str, content: bytes) -> None:
        bucket = self._build_bucket()
        await asyncio.to_thread(bucket.put_object, storage_key, content)

    async def read_bytes(self, storage_key: str) -> bytes:
        bucket = self._build_bucket()

        def _read() -> bytes:
            result = bucket.get_object(storage_key)
            return result.read()

        return await asyncio.to_thread(_read)

    async def exists_bytes(self, storage_key: str) -> bool:
        bucket = self._build_bucket()
        return await asyncio.to_thread(bucket.object_exists, storage_key)

    async def delete_bytes(self, storage_key: str) -> None:
        bucket = self._build_bucket()
        await asyncio.to_thread(bucket.delete_object, storage_key)


class WorkspaceArtifactsService:
    def __init__(self) -> None:
        self._backend_cache: Dict[Tuple[str, str], Any] = {}
        self._warned_missing_table = False

    def enabled(self) -> bool:
        return bool(getattr(config, "WORKSPACE_ARTIFACTS_ENABLED", True))

    def availability_state(self) -> str:
        if not self.enabled():
            return "disabled"
        if self._warned_missing_table:
            return "missing_table"
        return "enabled"

    def _configured_backend_name(self) -> str:
        backend_name = str(
            getattr(config, "WORKSPACE_ARTIFACTS_BACKEND", "local") or "local"
        ).strip()
        return backend_name or "local"

    def _configured_local_root(self) -> str:
        configured = str(getattr(config, "WORKSPACE_ARTIFACTS_LOCAL_ROOT", "") or "").strip()
        return configured or _default_local_root()

    def _configured_oss_prefix(self) -> str:
        prefix = str(getattr(config, "WORKSPACE_ARTIFACTS_OSS_PREFIX", "") or "").strip("/")
        return prefix or "workspace-artifacts"

    def _get_blob_backend(self, backend_name: Optional[str] = None):
        selected = str(backend_name or self._configured_backend_name()).strip() or "local"
        if selected == "local":
            cache_key = ("local", self._configured_local_root())
            cached = self._backend_cache.get(cache_key)
            if cached is None:
                cached = LocalArtifactBlobBackend(self._configured_local_root())
                self._backend_cache[cache_key] = cached
            return cached

        if selected == "aliyun_oss":
            endpoint = str(getattr(config, "WORKSPACE_ARTIFACTS_OSS_ENDPOINT", "") or "").strip()
            bucket_name = str(getattr(config, "WORKSPACE_ARTIFACTS_OSS_BUCKET", "") or "").strip()
            access_key_id = str(getattr(config, "WORKSPACE_ARTIFACTS_OSS_ACCESS_KEY_ID", "") or "").strip()
            access_key_secret = str(
                getattr(config, "WORKSPACE_ARTIFACTS_OSS_ACCESS_KEY_SECRET", "") or ""
            ).strip()
            if not all((endpoint, bucket_name, access_key_id, access_key_secret)):
                raise RuntimeError("Aliyun OSS artifact backend is not fully configured")
            cache_key = ("aliyun_oss", "|".join((endpoint, bucket_name, access_key_id)))
            cached = self._backend_cache.get(cache_key)
            if cached is None:
                cached = AliyunOSSArtifactBlobBackend(
                    endpoint=endpoint,
                    bucket_name=bucket_name,
                    access_key_id=access_key_id,
                    access_key_secret=access_key_secret,
                )
                self._backend_cache[cache_key] = cached
            return cached

        raise RuntimeError(f"Unsupported workspace artifact backend: {selected}")

    async def _get_client(self, client: Any = None):
        if client is not None:
            return client
        db = DBConnection()
        return await db.client

    @staticmethod
    def _is_missing_table_error(error: Exception) -> bool:
        message = str(error).lower()
        return "workspace_artifacts" in message and (
            "does not exist" in message or "undefined_table" in message
        )

    def _log_missing_table_once(self, error: Exception) -> None:
        if self._warned_missing_table:
            return
        self._warned_missing_table = True
        logger.warning(
            "workspace_artifacts table is missing; durable workspace continuity is disabled until migrations run: %s",
            error,
        )

    @staticmethod
    def _row_to_record(row: Any) -> WorkspaceArtifactRecord:
        metadata = _maybe_json_object(row.get("metadata"))
        created_at = row.get("created_at")
        updated_at = row.get("updated_at")
        return WorkspaceArtifactRecord(
            artifact_id=str(row.get("artifact_id") or ""),
            project_id=str(row.get("project_id") or ""),
            path=str(row.get("path") or ""),
            storage_backend=str(row.get("storage_backend") or ""),
            storage_key=str(row.get("storage_key") or ""),
            content_type=row.get("content_type"),
            size_bytes=int(row.get("size_bytes") or 0),
            sha256=str(row.get("sha256") or ""),
            source=str(row.get("source") or ""),
            sandbox_id=row.get("sandbox_id"),
            thread_id=row.get("thread_id"),
            agent_run_id=row.get("agent_run_id"),
            created_by_user_id=row.get("created_by_user_id"),
            metadata=metadata,
            created_at=str(created_at) if created_at is not None else None,
            updated_at=str(updated_at) if updated_at is not None else None,
        )

    def _build_storage_key(self, project_id: str, path: str, sha256_hex: str) -> str:
        basename = posixpath.basename(path) or "artifact"
        basename = _storage_key_component(basename, fallback="artifact")
        project_component = _storage_key_component(str(project_id or "project"), fallback="project")
        path_hash = hashlib.sha1(path.encode("utf-8")).hexdigest()
        if self._configured_backend_name() == "aliyun_oss":
            return "/".join(
                (
                    self._configured_oss_prefix(),
                    project_component,
                    path_hash[:2],
                    path_hash,
                    sha256_hex,
                    basename,
                )
            )
        return os.path.join(project_component, path_hash[:2], path_hash, sha256_hex, basename)

    @staticmethod
    def _row_sort_timestamp(row: Dict[str, Any]) -> str:
        return str(row.get("updated_at") or row.get("created_at") or "")

    def _dedupe_latest_rows_by_path(self, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        latest_by_path: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            path_value = str(row.get("path") or "")
            if not path_value:
                continue
            existing = latest_by_path.get(path_value)
            if existing is None or self._row_sort_timestamp(row) >= self._row_sort_timestamp(existing):
                latest_by_path[path_value] = row
        return sorted(latest_by_path.values(), key=lambda row: str(row.get("path") or ""))

    async def _blob_exists(
        self,
        *,
        project_id: str,
        path: str,
        storage_backend: str,
        storage_key: str,
    ) -> bool:
        normalized_backend = str(storage_backend or "").strip()
        normalized_key = str(storage_key or "").strip()
        if not normalized_backend or not normalized_key:
            return False

        backend = self._get_blob_backend(normalized_backend)
        exists_bytes = getattr(backend, "exists_bytes", None)
        if not callable(exists_bytes):
            return True

        try:
            return bool(await exists_bytes(normalized_key))
        except FileNotFoundError:
            return False
        except Exception as backend_error:
            logger.warning(
                "Failed to verify workspace artifact blob existence project=%s path=%s backend=%s key=%s: %s",
                project_id,
                path,
                normalized_backend,
                normalized_key,
                backend_error,
            )
            return True

    async def persist_artifact(
        self,
        *,
        project_id: str,
        path: str,
        content: Any,
        source: str,
        client: Any = None,
        thread_id: Optional[str] = None,
        agent_run_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        allow_hidden: bool = False,
    ) -> Optional[WorkspaceArtifactRecord]:
        if not self.enabled():
            return None

        normalized_path = normalize_workspace_artifact_path(path, allow_root=False)
        if not allow_hidden and is_hidden_workspace_artifact_path(normalized_path):
            return None

        client = await self._get_client(client)
        existing_record = await self.get_artifact_record(
            project_id=project_id,
            path=normalized_path,
            client=client,
            include_hidden=True,
        )
        data = _coerce_content_bytes(content)
        sha256_hex = hashlib.sha256(data).hexdigest()
        selected_backend = self._configured_backend_name()
        backend = self._get_blob_backend(selected_backend)
        storage_key = self._build_storage_key(project_id, normalized_path, sha256_hex)
        artifact_metadata = dict(metadata or {})
        sandbox_hint_aliases = _build_sandbox_hint_aliases(
            sandbox_id=sandbox_id,
            metadata=artifact_metadata,
            existing_record=existing_record,
        )
        if sandbox_hint_aliases:
            artifact_metadata["sandbox_id_aliases"] = sandbox_hint_aliases
        resolved_content_type = guess_workspace_artifact_content_type(
            normalized_path,
            content,
            content_type,
        )

        await backend.write_bytes(storage_key, data)

        workspace_scope = str(artifact_metadata.get("workspace_scope") or "").strip()
        if workspace_scope == "agent_run" and agent_run_id:
            conflict_clause = """
            ON CONFLICT (project_id, agent_run_id, path)
            WHERE (metadata ->> 'workspace_scope') = 'agent_run'
              AND agent_run_id IS NOT NULL
            DO UPDATE SET
                thread_id = EXCLUDED.thread_id,
                storage_backend = EXCLUDED.storage_backend,
                storage_key = EXCLUDED.storage_key,
                content_type = EXCLUDED.content_type,
                size_bytes = EXCLUDED.size_bytes,
                sha256 = EXCLUDED.sha256,
                source = EXCLUDED.source,
                sandbox_id = COALESCE(EXCLUDED.sandbox_id, workspace_artifacts.sandbox_id),
                created_by_user_id = EXCLUDED.created_by_user_id,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """
        else:
            conflict_clause = """
            ON CONFLICT (project_id, path)
            WHERE COALESCE(metadata ->> 'workspace_scope', 'project_current') <> 'agent_run'
            DO UPDATE SET
                thread_id = EXCLUDED.thread_id,
                agent_run_id = EXCLUDED.agent_run_id,
                storage_backend = EXCLUDED.storage_backend,
                storage_key = EXCLUDED.storage_key,
                content_type = EXCLUDED.content_type,
                size_bytes = EXCLUDED.size_bytes,
                sha256 = EXCLUDED.sha256,
                source = EXCLUDED.source,
                sandbox_id = COALESCE(EXCLUDED.sandbox_id, workspace_artifacts.sandbox_id),
                created_by_user_id = EXCLUDED.created_by_user_id,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """

        query = f"""
            INSERT INTO workspace_artifacts (
                project_id,
                thread_id,
                agent_run_id,
                path,
                storage_backend,
                storage_key,
                content_type,
                size_bytes,
                sha256,
                source,
                sandbox_id,
                created_by_user_id,
                metadata
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb
            )
            {conflict_clause}
            RETURNING *
        """

        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    str(project_id),
                    str(thread_id) if thread_id else None,
                    str(agent_run_id) if agent_run_id else None,
                    normalized_path,
                    selected_backend,
                    storage_key,
                    resolved_content_type,
                    len(data),
                    sha256_hex,
                    str(source or "unknown"),
                    str(sandbox_id) if sandbox_id else None,
                    str(created_by_user_id) if created_by_user_id else None,
                    json.dumps(artifact_metadata, ensure_ascii=False),
                )
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return None
            logger.warning(
                "Failed to persist workspace artifact project=%s path=%s: %s",
                project_id,
                normalized_path,
                db_error,
            )
            raise

        return self._row_to_record(dict(row))

    async def get_artifact_record(
        self,
        *,
        project_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
        agent_run_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ) -> Optional[WorkspaceArtifactRecord]:
        if not self.enabled():
            return None

        normalized_path = normalize_workspace_artifact_path(path)
        if not include_hidden and is_hidden_workspace_artifact_path(normalized_path):
            return None

        client = await self._get_client(client)
        filters = ["project_id = $1", "path = $2"]
        params: List[Any] = [str(project_id), normalized_path]
        if agent_run_id:
            params.append(str(agent_run_id))
            filters.append(f"agent_run_id = ${len(params)}")
            filters.append("(metadata ->> 'workspace_scope') = 'agent_run'")
            filters.append("agent_run_id IS NOT NULL")
        if sandbox_id:
            params.append(str(sandbox_id))
            filters.append(f"sandbox_id = ${len(params)}")
        if not agent_run_id and not sandbox_id:
            filters.append("COALESCE(metadata ->> 'workspace_scope', 'project_current') <> 'agent_run'")
        query = f"""
            SELECT *
            FROM workspace_artifacts
            WHERE {' AND '.join(filters)}
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT 1
        """
        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(query, *params)
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return None
            raise
        if row is None:
            return None
        return self._row_to_record(dict(row))

    async def read_artifact_bytes(
        self,
        *,
        project_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
        agent_run_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ) -> Optional[Tuple[WorkspaceArtifactRecord, bytes]]:
        record = await self.get_artifact_record(
            project_id=project_id,
            path=path,
            client=client,
            include_hidden=include_hidden,
            agent_run_id=agent_run_id,
            sandbox_id=sandbox_id,
        )
        if record is None:
            return None

        backend = self._get_blob_backend(record.storage_backend)
        try:
            data = await backend.read_bytes(record.storage_key)
        except FileNotFoundError:
            return None
        except Exception as backend_error:
            if not await self._blob_exists(
                project_id=str(project_id),
                path=record.path,
                storage_backend=record.storage_backend,
                storage_key=record.storage_key,
            ):
                return None
            logger.warning(
                "Failed to read workspace artifact blob project=%s path=%s backend=%s key=%s: %s",
                project_id,
                record.path,
                record.storage_backend,
                record.storage_key,
                backend_error,
            )
            raise
        return record, data

    async def has_artifacts_for_run(
        self,
        agent_run_id: str,
        *,
        client: Any = None,
    ) -> bool:
        """Return True if the workspace_artifacts table has entries for a run."""
        if not self.enabled():
            return False
        client = await self._get_client(client)
        query = """
            SELECT artifact_id
            FROM workspace_artifacts
            WHERE agent_run_id = $1
              AND (metadata ->> 'workspace_scope') = 'agent_run'
              AND agent_run_id IS NOT NULL
            LIMIT 1
        """
        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(query, str(agent_run_id))
            return row is not None
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return False
            return False

    async def has_artifacts_for_thread(
        self,
        *,
        project_id: str,
        thread_id: str,
        client: Any = None,
    ) -> bool:
        """Return True if a thread has any run-scoped workspace artifacts."""
        if not self.enabled():
            return False
        client = await self._get_client(client)
        query = """
            SELECT artifact_id
            FROM workspace_artifacts
            WHERE project_id = $1
              AND thread_id = $2
              AND (metadata ->> 'workspace_scope') = 'agent_run'
              AND agent_run_id IS NOT NULL
            LIMIT 1
        """
        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(query, str(project_id), str(thread_id))
            return row is not None
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
            return False

    async def resolve_project_for_sandbox_hints(
        self,
        *,
        sandbox_ids: Sequence[str],
        client: Any = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled():
            return None

        normalized_sandbox_ids = [
            str(sandbox_id).strip()
            for sandbox_id in sandbox_ids
            if str(sandbox_id or "").strip()
        ]
        if not normalized_sandbox_ids:
            return None

        client = await self._get_client(client)
        claude_local_run_id = _parse_claude_local_agent_run_id(*normalized_sandbox_ids)
        if claude_local_run_id:
            sandbox_id = f"{_CLAUDE_LOCAL_SANDBOX_PREFIX}{claude_local_run_id}"
            query = """
                SELECT project_id, sandbox_id, updated_at, created_at
                FROM workspace_artifacts
                WHERE sandbox_id = $1
                  AND agent_run_id = $2
                  AND (metadata ->> 'workspace_scope') = 'agent_run'
                  AND agent_run_id IS NOT NULL
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
            """
            try:
                async with client.pool.acquire() as conn:
                    row = await conn.fetchrow(query, sandbox_id, claude_local_run_id)
            except Exception as db_error:
                if self._is_missing_table_error(db_error):
                    self._log_missing_table_once(db_error)
                    return None
                raise
            if row is None:
                query = """
                    SELECT project_id, sandbox_id, updated_at, created_at
                    FROM workspace_artifacts
                    WHERE agent_run_id = $1
                      AND (metadata ->> 'workspace_scope') = 'agent_run'
                      AND agent_run_id IS NOT NULL
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                    LIMIT 1
                """
                try:
                    async with client.pool.acquire() as conn:
                        row = await conn.fetchrow(query, claude_local_run_id)
                except Exception as db_error:
                    if self._is_missing_table_error(db_error):
                        self._log_missing_table_once(db_error)
                        return None
                    raise
                if row is None:
                    return None
            return self._project_hint_from_row(row)

        query = """
            SELECT project_id, sandbox_id, updated_at, created_at
            FROM workspace_artifacts
            WHERE sandbox_id = ANY($1::text[])
               OR COALESCE(metadata ->> 'requested_sandbox_id', '') = ANY($1::text[])
               OR COALESCE(metadata ->> 'resolved_sandbox_id', '') = ANY($1::text[])
               OR COALESCE(metadata ->> 'project_sandbox_id', '') = ANY($1::text[])
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements_text(
                       CASE
                           WHEN jsonb_typeof(metadata -> 'sandbox_id_aliases') = 'array'
                           THEN metadata -> 'sandbox_id_aliases'
                           ELSE '[]'::jsonb
                       END
                   ) AS alias(value)
                   WHERE alias.value = ANY($1::text[])
               )
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT 1
        """
        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(query, normalized_sandbox_ids)
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return None
            raise
        if row is None:
            return None

        return self._project_hint_from_row(row)

    @staticmethod
    def _project_hint_from_row(row: Any) -> Optional[Dict[str, Any]]:
        result = dict(row)
        project_id = str(result.get("project_id") or "").strip()
        if not project_id:
            return None
        sandbox_id = str(result.get("sandbox_id") or "").strip() or None
        updated_at = result.get("updated_at")
        created_at = result.get("created_at")
        return {
            "project_id": project_id,
            "sandbox_id": sandbox_id,
            "updated_at": str(updated_at) if updated_at is not None else None,
            "created_at": str(created_at) if created_at is not None else None,
        }

    async def delete_artifact(
        self,
        *,
        project_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
    ) -> Optional[WorkspaceArtifactRecord]:
        if not self.enabled():
            return None

        normalized_path = normalize_workspace_artifact_path(path)
        if not include_hidden and is_hidden_workspace_artifact_path(normalized_path):
            return None

        client = await self._get_client(client)
        query = """
            DELETE FROM workspace_artifacts
            WHERE project_id = $1
              AND path = $2
            RETURNING *
        """
        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(query, str(project_id), normalized_path)
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return None
            raise
        if row is None:
            return None

        record = self._row_to_record(dict(row))
        backend = self._get_blob_backend(record.storage_backend)
        try:
            await backend.delete_bytes(record.storage_key)
        except Exception as backend_error:
            logger.warning(
                "Failed to delete workspace artifact blob project=%s path=%s backend=%s key=%s: %s",
                project_id,
                record.path,
                record.storage_backend,
                record.storage_key,
                backend_error,
            )
        return record

    async def _fetch_rows_for_prefix(
        self,
        *,
        project_id: str,
        root_path: str,
        client: Any = None,
        include_hidden: bool = False,
        agent_run_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.enabled():
            return []

        normalized_root = normalize_workspace_artifact_path(root_path)
        client = await self._get_client(client)
        filters = ["project_id = $1"]
        params: List[Any] = [str(project_id)]
        if normalized_root == WORKSPACE_ROOT:
            params.append(f"{WORKSPACE_ROOT}/%")
            filters.append(f"path LIKE ${len(params)}")
        else:
            params.append(normalized_root)
            exact_param = len(params)
            params.append(f"{normalized_root.rstrip('/')}/%")
            prefix_param = len(params)
            filters.append(f"(path = ${exact_param} OR path LIKE ${prefix_param})")
        if agent_run_id:
            params.append(str(agent_run_id))
            filters.append(f"agent_run_id = ${len(params)}")
            filters.append("(metadata ->> 'workspace_scope') = 'agent_run'")
            filters.append("agent_run_id IS NOT NULL")
        if sandbox_id:
            params.append(str(sandbox_id))
            filters.append(f"sandbox_id = ${len(params)}")
        if not agent_run_id and not sandbox_id:
            filters.append("COALESCE(metadata ->> 'workspace_scope', 'project_current') <> 'agent_run'")

        query = f"""
            SELECT *
            FROM workspace_artifacts
            WHERE {' AND '.join(filters)}
            ORDER BY path ASC
        """

        try:
            async with client.pool.acquire() as conn:
                rows = await conn.fetch(query, *params)
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return []
            raise

        result = [dict(row) for row in rows]
        if include_hidden:
            return result
        return [
            row
            for row in result
            if not is_hidden_workspace_artifact_path(str(row.get("path") or ""))
        ]

    async def _fetch_thread_rows_for_prefix(
        self,
        *,
        project_id: str,
        thread_id: str,
        root_path: str,
        client: Any = None,
        include_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        if not self.enabled():
            return []

        normalized_root = normalize_workspace_artifact_path(root_path)
        client = await self._get_client(client)
        filters = [
            "project_id = $1",
            "thread_id = $2",
            "(metadata ->> 'workspace_scope') = 'agent_run'",
            "agent_run_id IS NOT NULL",
        ]
        params: List[Any] = [str(project_id), str(thread_id)]
        if normalized_root == WORKSPACE_ROOT:
            params.append(f"{WORKSPACE_ROOT}/%")
            filters.append(f"path LIKE ${len(params)}")
        else:
            params.append(normalized_root)
            exact_param = len(params)
            params.append(f"{normalized_root.rstrip('/')}/%")
            prefix_param = len(params)
            filters.append(f"(path = ${exact_param} OR path LIKE ${prefix_param})")

        query = f"""
            SELECT *
            FROM workspace_artifacts
            WHERE {' AND '.join(filters)}
            ORDER BY path ASC, updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        """

        try:
            async with client.pool.acquire() as conn:
                rows = await conn.fetch(query, *params)
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return []
            raise

        deduped = self._dedupe_latest_rows_by_path(dict(row) for row in rows)
        if include_hidden:
            return deduped
        return [
            row
            for row in deduped
            if not is_hidden_workspace_artifact_path(str(row.get("path") or ""))
        ]

    async def list_entries(
        self,
        *,
        project_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
        agent_run_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_path = normalize_workspace_artifact_path(path)
        if not include_hidden and is_hidden_workspace_artifact_path(normalized_path):
            return []

        rows = await self._fetch_rows_for_prefix(
            project_id=project_id,
            root_path=normalized_path,
            client=client,
            include_hidden=include_hidden,
            agent_run_id=agent_run_id,
            sandbox_id=sandbox_id,
        )
        if not rows:
            return []

        prefix = f"{normalized_path.rstrip('/')}/"
        if normalized_path == WORKSPACE_ROOT:
            prefix = f"{WORKSPACE_ROOT}/"

        children: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            current_path = str(row.get("path") or "")
            if current_path == normalized_path:
                continue
            if not current_path.startswith(prefix):
                continue
            relative = current_path[len(prefix):]
            if not relative:
                continue
            head, _, tail = relative.partition("/")
            child_path = f"{normalized_path.rstrip('/')}/{head}"
            if normalized_path == WORKSPACE_ROOT:
                child_path = f"{WORKSPACE_ROOT}/{head}"

            updated_at = row.get("updated_at")
            updated_at_text = str(updated_at) if updated_at is not None else ""
            if tail:
                existing = children.get(child_path)
                if existing is None:
                    children[child_path] = {
                        "name": head,
                        "path": child_path,
                        "is_dir": True,
                        "size": 0,
                        "mod_time": updated_at_text,
                        "permissions": None,
                        "delivery_source": "artifact",
                        "downloadable": False,
                    }
                elif updated_at_text and updated_at_text > str(existing.get("mod_time") or ""):
                    existing["mod_time"] = updated_at_text
                continue

            blob_available = await self._blob_exists(
                project_id=str(project_id),
                path=current_path,
                storage_backend=str(row.get("storage_backend") or ""),
                storage_key=str(row.get("storage_key") or ""),
            )
            artifact_source = str(row.get("source") or "").strip() or None

            children[child_path] = {
                "name": head,
                "path": current_path,
                "is_dir": False,
                "size": int(row.get("size_bytes") or 0),
                "mod_time": updated_at_text,
                "permissions": None,
                "delivery_source": "artifact",
                "downloadable": blob_available,
                "artifact_state": "available" if blob_available else "missing_blob",
                "artifact_source": artifact_source,
            }

        return sorted(
            children.values(),
            key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").lower()),
        )

    async def list_thread_entries(
        self,
        *,
        project_id: str,
        thread_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        normalized_path = normalize_workspace_artifact_path(path)
        if not include_hidden and is_hidden_workspace_artifact_path(normalized_path):
            return []

        rows = await self._fetch_thread_rows_for_prefix(
            project_id=project_id,
            thread_id=thread_id,
            root_path=normalized_path,
            client=client,
            include_hidden=include_hidden,
        )
        if not rows:
            return []

        prefix = f"{normalized_path.rstrip('/')}/"
        if normalized_path == WORKSPACE_ROOT:
            prefix = f"{WORKSPACE_ROOT}/"

        children: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            current_path = str(row.get("path") or "")
            if current_path == normalized_path:
                continue
            if not current_path.startswith(prefix):
                continue
            relative = current_path[len(prefix):]
            if not relative:
                continue
            head, _, tail = relative.partition("/")
            child_path = f"{normalized_path.rstrip('/')}/{head}"
            if normalized_path == WORKSPACE_ROOT:
                child_path = f"{WORKSPACE_ROOT}/{head}"

            updated_at = row.get("updated_at")
            updated_at_text = str(updated_at) if updated_at is not None else ""
            if tail:
                existing = children.get(child_path)
                if existing is None:
                    children[child_path] = {
                        "name": head,
                        "path": child_path,
                        "is_dir": True,
                        "size": 0,
                        "mod_time": updated_at_text,
                        "permissions": None,
                        "delivery_source": "thread_workspace",
                        "downloadable": False,
                    }
                elif updated_at_text and updated_at_text > str(existing.get("mod_time") or ""):
                    existing["mod_time"] = updated_at_text
                continue

            blob_available = await self._blob_exists(
                project_id=str(project_id),
                path=current_path,
                storage_backend=str(row.get("storage_backend") or ""),
                storage_key=str(row.get("storage_key") or ""),
            )
            artifact_source = str(row.get("source") or "").strip() or None
            children[child_path] = {
                "name": head,
                "path": current_path,
                "is_dir": False,
                "size": int(row.get("size_bytes") or 0),
                "mod_time": updated_at_text,
                "permissions": None,
                "delivery_source": "thread_workspace",
                "downloadable": blob_available,
                "artifact_state": "available" if blob_available else "missing_blob",
                "artifact_source": artifact_source,
                "source_run_id": row.get("agent_run_id"),
            }

        return sorted(
            children.values(),
            key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").lower()),
        )

    async def collect_file_paths(
        self,
        *,
        project_id: str,
        root_path: str,
        client: Any = None,
        include_hidden: bool = False,
        agent_run_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
    ) -> List[str]:
        normalized_root = normalize_workspace_artifact_path(root_path)
        rows = await self._fetch_rows_for_prefix(
            project_id=project_id,
            root_path=normalized_root,
            client=client,
            include_hidden=include_hidden,
            agent_run_id=agent_run_id,
            sandbox_id=sandbox_id,
        )
        deduped: List[str] = []
        seen: set[str] = set()
        for row in rows:
            path_value = str(row.get("path") or "")
            if path_value in seen:
                continue
            seen.add(path_value)
            deduped.append(path_value)
        return deduped

    async def collect_thread_file_paths(
        self,
        *,
        project_id: str,
        thread_id: str,
        root_path: str,
        client: Any = None,
        include_hidden: bool = False,
    ) -> List[str]:
        normalized_root = normalize_workspace_artifact_path(root_path)
        rows = await self._fetch_thread_rows_for_prefix(
            project_id=project_id,
            thread_id=thread_id,
            root_path=normalized_root,
            client=client,
            include_hidden=include_hidden,
        )
        deduped: List[str] = []
        seen: set[str] = set()
        for row in rows:
            path_value = str(row.get("path") or "")
            if path_value in seen:
                continue
            seen.add(path_value)
            deduped.append(path_value)
        return deduped

    async def list_records(
        self,
        *,
        project_id: str,
        root_path: str = WORKSPACE_ROOT,
        client: Any = None,
        include_hidden: bool = False,
    ) -> List[WorkspaceArtifactRecord]:
        rows = await self._fetch_rows_for_prefix(
            project_id=project_id,
            root_path=root_path,
            client=client,
            include_hidden=include_hidden,
        )
        return [self._row_to_record(row) for row in rows]

    async def rehydrate_project(
        self,
        *,
        project_id: str,
        write_file: Callable[[str, bytes], Awaitable[None]],
        make_dir: Optional[Callable[[str], Awaitable[None]]] = None,
        client: Any = None,
        include_hidden: bool = False,
    ) -> Dict[str, Any]:
        records = await self.list_records(
            project_id=project_id,
            root_path=WORKSPACE_ROOT,
            client=client,
            include_hidden=include_hidden,
        )
        if not records:
            return {"total": 0, "rehydrated": 0, "failed": 0, "failures": []}

        created_dirs = {WORKSPACE_ROOT}
        failures: List[Dict[str, str]] = []
        rehydrated = 0

        for record in records:
            parent = posixpath.dirname(record.path)
            if make_dir is not None and parent and parent.startswith(WORKSPACE_ROOT):
                pending_dirs: List[str] = []
                current = parent
                while current and current.startswith(WORKSPACE_ROOT) and current not in created_dirs:
                    pending_dirs.append(current)
                    if current == WORKSPACE_ROOT:
                        break
                    current = posixpath.dirname(current)

                for directory in reversed(pending_dirs):
                    try:
                        await make_dir(directory)
                    except Exception as make_dir_error:
                        failures.append({"path": directory, "error": str(make_dir_error)})
                        logger.warning(
                            "Failed to create workspace artifact directory during rehydrate project=%s dir=%s: %s",
                            project_id,
                            directory,
                            make_dir_error,
                        )
                    created_dirs.add(directory)

            try:
                blob = await self._get_blob_backend(record.storage_backend).read_bytes(record.storage_key)
                await write_file(record.path, blob)
                rehydrated += 1
            except Exception as write_error:
                failures.append({"path": record.path, "error": str(write_error)})
                logger.warning(
                    "Failed to rehydrate workspace artifact project=%s path=%s: %s",
                    project_id,
                    record.path,
                    write_error,
                )

        return {
            "total": len(records),
            "rehydrated": rehydrated,
            "failed": len(failures),
            "failures": failures,
        }

    async def get_thread_artifact_record(
        self,
        *,
        project_id: str,
        thread_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
    ) -> Optional[WorkspaceArtifactRecord]:
        if not self.enabled():
            return None

        normalized_path = normalize_workspace_artifact_path(path, allow_root=False)
        if not include_hidden and is_hidden_workspace_artifact_path(normalized_path):
            return None

        client = await self._get_client(client)
        query = """
            SELECT *
            FROM workspace_artifacts
            WHERE project_id = $1
              AND thread_id = $2
              AND path = $3
              AND (metadata ->> 'workspace_scope') = 'agent_run'
              AND agent_run_id IS NOT NULL
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT 1
        """
        try:
            async with client.pool.acquire() as conn:
                row = await conn.fetchrow(query, str(project_id), str(thread_id), normalized_path)
        except Exception as db_error:
            if self._is_missing_table_error(db_error):
                self._log_missing_table_once(db_error)
                return None
            raise
        if row is None:
            return None
        return self._row_to_record(dict(row))

    async def read_thread_artifact_bytes(
        self,
        *,
        project_id: str,
        thread_id: str,
        path: str,
        client: Any = None,
        include_hidden: bool = False,
    ) -> Optional[Tuple[WorkspaceArtifactRecord, bytes]]:
        record = await self.get_thread_artifact_record(
            project_id=project_id,
            thread_id=thread_id,
            path=path,
            client=client,
            include_hidden=include_hidden,
        )
        if record is None:
            return None
        data = await self._get_blob_backend(record.storage_backend).read_bytes(record.storage_key)
        return record, data

    async def rehydrate_thread(
        self,
        *,
        project_id: str,
        thread_id: str,
        write_file: Callable[[str, bytes], Awaitable[None]],
        make_dir: Optional[Callable[[str], Awaitable[None]]] = None,
        client: Any = None,
        include_hidden: bool = False,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_total_bytes: int = 200 * 1024 * 1024,
    ) -> Dict[str, Any]:
        rows = await self._fetch_thread_rows_for_prefix(
            project_id=project_id,
            thread_id=thread_id,
            root_path=WORKSPACE_ROOT,
            client=client,
            include_hidden=include_hidden,
        )
        records = [self._row_to_record(row) for row in rows]
        if not records:
            return {"total": 0, "rehydrated": 0, "failed": 0, "failures": []}

        created_dirs = {WORKSPACE_ROOT}
        failures: List[Dict[str, str]] = []
        rehydrated = 0
        total_bytes = 0

        for record in records:
            if record.size_bytes > max_file_bytes:
                failures.append({"path": record.path, "error": "file too large for thread workspace rehydrate"})
                continue
            if total_bytes + record.size_bytes > max_total_bytes:
                failures.append({"path": record.path, "error": "thread workspace rehydrate byte limit exceeded"})
                continue

            parent = posixpath.dirname(record.path)
            if make_dir is not None and parent and parent.startswith(WORKSPACE_ROOT):
                pending_dirs: List[str] = []
                current = parent
                while current and current.startswith(WORKSPACE_ROOT) and current not in created_dirs:
                    pending_dirs.append(current)
                    if current == WORKSPACE_ROOT:
                        break
                    current = posixpath.dirname(current)
                for directory in reversed(pending_dirs):
                    try:
                        await make_dir(directory)
                    except Exception as make_dir_error:
                        failures.append({"path": directory, "error": str(make_dir_error)})
                    created_dirs.add(directory)

            try:
                blob = await self._get_blob_backend(record.storage_backend).read_bytes(record.storage_key)
                await write_file(record.path, blob)
                total_bytes += len(blob)
                rehydrated += 1
            except Exception as write_error:
                failures.append({"path": record.path, "error": str(write_error)})

        return {
            "total": len(records),
            "rehydrated": rehydrated,
            "failed": len(failures),
            "failures": failures,
        }


workspace_artifacts = WorkspaceArtifactsService()
