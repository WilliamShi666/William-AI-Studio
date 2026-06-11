import json
import os
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from sandbox import api as sandbox_api


@dataclass
class _FakeEntry:
    name: str
    is_dir: bool = False
    size: int = 0
    mod_time: str = ""


class _DummyDB:
    @property
    def client(self):
        async def _client():
            return object()

        return _client()


class _CapturingLogger:
    def __init__(self):
        self.records = []

    def info(self, *args, **kwargs):
        self.records.append(("info", args, kwargs))

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args, kwargs))

    def error(self, *args, **kwargs):
        self.records.append(("error", args, kwargs))

    def debug(self, *args, **kwargs):
        self.records.append(("debug", args, kwargs))


class _FakeFiles:
    def list(self, path: str):
        if path == "/workspace":
            return [
                _FakeEntry(name="ok.txt", size=2),
                _FakeEntry(name="bad.txt", size=2),
                _FakeEntry(name="data", is_dir=True),
            ]
        if path == "/workspace/data":
            return [_FakeEntry(name="nested.md", size=6)]
        raise FileNotFoundError(path)

    def read(self, path: str, format: str = "bytes"):
        if path == "/workspace/ok.txt":
            return b"ok"
        if path == "/workspace/data/nested.md":
            return "nested"
        raise FileNotFoundError(path)


class _AlwaysFailFiles:
    def list(self, path: str):
        if path == "/workspace":
            return [_FakeEntry(name="bad.txt", size=2)]
        raise FileNotFoundError(path)

    def read(self, path: str, format: str = "bytes"):
        raise FileNotFoundError(path)


class _FakeSandbox:
    def __init__(self, files):
        self.files = files


@pytest.fixture(autouse=True)
def _sandbox_api_test_setup(monkeypatch):
    monkeypatch.setattr(sandbox_api, "db", _DummyDB())

    async def _verify_access(*args, **kwargs):
        return {
            "project_id": "proj-1",
            "sandbox": json.dumps({"id": "sb-test", "type": "desktop", "state": "running"}),
        }

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    async def _artifact_read(*args, **kwargs):
        return None

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)


@pytest.mark.asyncio
async def test_read_file_timeout_returns_504(monkeypatch):
    async def _get_project_sandbox(*args, **kwargs):
        return _FakeSandbox(_FakeFiles()), "sb-timeout", None

    async def _timed_out(*args, **kwargs):
        raise sandbox_api.SandboxIOTimeoutError(
            operation="files.read",
            timeout_seconds=0.1,
            sandbox_id="sb-timeout",
            path="/workspace/timeout.txt",
        )

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_project_sandbox)
    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _timed_out)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._read_file_internal("sb-timeout", "/workspace/timeout.txt", None, "user-1")

    assert exc_info.value.status_code == 504


@pytest.mark.asyncio
async def test_read_file_queue_timeout_returns_429(monkeypatch):
    async def _get_project_sandbox(*args, **kwargs):
        return _FakeSandbox(_FakeFiles()), "sb-queue", None

    async def _queue_timeout(*args, **kwargs):
        raise sandbox_api.SandboxIOQueueTimeoutError(
            operation="files.read",
            queue_timeout_seconds=0.1,
            sandbox_id="sb-queue",
            path="/workspace/queue.txt",
        )

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_project_sandbox)
    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _queue_timeout)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._read_file_internal("sb-queue", "/workspace/queue.txt", None, "user-1")

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers.get("Retry-After")


@pytest.mark.asyncio
async def test_archive_partial_success_generates_report(monkeypatch):
    async def _get_project_sandbox(*args, **kwargs):
        return _FakeSandbox(_FakeFiles()), "sb-archive", None

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_project_sandbox)

    async def _direct_guarded(*args, **kwargs):
        call = args[2]
        return call()

    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _direct_guarded)

    response = await sandbox_api.download_files_archive(
        sandbox_id="sb-archive",
        payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
        background_tasks=BackgroundTasks(),
        request=None,
        user_id="user-1",
    )

    assert response.media_type == "application/zip"
    assert response.headers.get("x-archive-total") == "3"
    assert response.headers.get("x-archive-succeeded") == "2"
    assert response.headers.get("x-archive-failed") == "1"
    assert response.headers.get("x-archive-response-source") == "sandbox"
    assert response.headers.get("x-archive-fallback") == "0"
    assert response.headers.get("x-archive-sandbox-available") == "1"
    assert response.headers.get("x-archive-outcome") == "partial_success"

    archive_path = response.path
    assert archive_path
    try:
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as archive:
            names = set(archive.namelist())
            assert "ok.txt" in names
            assert "data/nested.md" in names
            assert "_download_report.json" in names

            report = json.loads(archive.read("_download_report.json"))
            assert report["summary"]["total"] == 3
            assert report["summary"]["succeeded"] == 2
            assert report["summary"]["failed"] == 1
            assert len(report["failed_files"]) == 1
    finally:
        if archive_path and os.path.exists(archive_path):
            os.remove(archive_path)


@pytest.mark.asyncio
async def test_archive_all_fail_returns_424(monkeypatch):
    async def _get_project_sandbox(*args, **kwargs):
        return _FakeSandbox(_AlwaysFailFiles()), "sb-all-fail", None

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_project_sandbox)

    async def _direct_guarded(*args, **kwargs):
        call = args[2]
        return call()

    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _direct_guarded)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api.download_files_archive(
            sandbox_id="sb-all-fail",
            payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
            background_tasks=BackgroundTasks(),
            request=SimpleNamespace(
                headers={
                    "x-request-id": "req-archive-all-fail",
                    "x-client-operation-id": "file-archive-all-fail",
                },
                state=SimpleNamespace(),
            ),
            user_id="user-1",
        )

    assert exc_info.value.status_code == 424
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["report"]["summary"]["succeeded"] == 0
    assert exc_info.value.headers.get("X-Request-Id") == "req-archive-all-fail"
    assert exc_info.value.headers.get("X-Client-Operation-Id") == "file-archive-all-fail"
    assert exc_info.value.headers.get("X-Archive-Outcome") == "error"
    assert exc_info.value.headers.get("X-Archive-Sandbox-Available") == "1"




@pytest.mark.asyncio
async def test_claude_local_archive_filters_artifacts_to_requested_run(monkeypatch):
    requested_sandbox_id = "claude-local:run-archive"
    collect_calls = []
    read_calls = []

    async def _verify_access(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Synthetic sandbox not found")

    async def _artifact_hint(*, sandbox_ids, client=None):
        assert requested_sandbox_id in sandbox_ids
        return {"project_id": "proj-archive", "sandbox_id": requested_sandbox_id}

    async def _load_project(_client, project_id: str):
        assert project_id == "proj-archive"
        return {
            "project_id": "proj-archive",
            "account_id": "user-1",
            "sandbox": json.dumps({"id": "sb-project", "type": "desktop", "state": "running"}),
        }

    async def _collect_file_paths(**kwargs):
        collect_calls.append(kwargs)
        assert kwargs["agent_run_id"] == "run-archive"
        return ["/workspace/run-archive.txt"]

    async def _read_artifact_bytes(**kwargs):
        read_calls.append(kwargs)
        assert kwargs["agent_run_id"] == "run-archive"
        return SimpleNamespace(content_type="text/plain"), b"archive-body"

    async def _no_live_sandbox(*args, **kwargs):
        raise AssertionError("claude-local archive must not attach live sandbox")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "resolve_project_for_sandbox_hints", _artifact_hint)
    monkeypatch.setattr(sandbox_api, "_load_project_by_id", _load_project)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "collect_file_paths", _collect_file_paths)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _read_artifact_bytes)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _no_live_sandbox)

    response = await sandbox_api.download_files_archive(
        sandbox_id=requested_sandbox_id,
        payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
        background_tasks=BackgroundTasks(),
        request=None,
        user_id="user-1",
    )

    assert collect_calls
    assert read_calls
    assert response.headers.get("x-archive-response-source") == "artifact"
    assert response.headers.get("x-archive-succeeded") == "1"

    archive_path = response.path
    try:
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as archive:
            assert archive.read("run-archive.txt") == b"archive-body"
    finally:
        if archive_path and os.path.exists(archive_path):
            os.remove(archive_path)

@pytest.mark.asyncio
async def test_archive_succeeds_with_artifacts_only_when_sandbox_unavailable(monkeypatch):
    async def _unavailable_sandbox(*args, **kwargs):
        raise RuntimeError("sandbox attach failed")

    async def _artifact_paths(*args, **kwargs):
        return ["/workspace/report.txt"]

    async def _artifact_read(*args, **kwargs):
        return SimpleNamespace(content_type="text/plain"), b"artifact-report"

    logger = _CapturingLogger()

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _unavailable_sandbox)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "collect_file_paths", _artifact_paths)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)
    monkeypatch.setattr(sandbox_api, "logger", logger)

    response = await sandbox_api.download_files_archive(
        sandbox_id="sb-archive",
        payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
        background_tasks=BackgroundTasks(),
        request=SimpleNamespace(
            headers={
                "x-request-id": "req-archive-artifact",
                "x-client-operation-id": "file-archive-test-1",
            },
            state=SimpleNamespace(),
        ),
        user_id="user-1",
    )

    assert response.headers.get("x-request-id") == "req-archive-artifact"
    assert response.headers.get("x-client-operation-id") == "file-archive-test-1"
    assert response.headers.get("x-archive-total") == "1"
    assert response.headers.get("x-archive-succeeded") == "1"
    assert response.headers.get("x-archive-failed") == "0"
    assert response.headers.get("x-archive-response-source") == "artifact"
    assert response.headers.get("x-archive-fallback") == "1"
    assert response.headers.get("x-archive-identity-source") == "sandbox_access"
    assert response.headers.get("x-archive-sandbox-available") == "0"
    assert response.headers.get("x-archive-outcome") == "success"

    archive_path = response.path
    assert archive_path
    try:
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as archive:
            assert archive.read("report.txt") == b"artifact-report"
            report = json.loads(archive.read("_download_report.json"))
            assert report["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
            assert report["sandbox_available"] is False
    finally:
        if archive_path and os.path.exists(archive_path):
            os.remove(archive_path)

    completion = [
        kwargs.get("extra", {})
        for level, args, kwargs in logger.records
        if level == "info" and args and args[0] == "Archive generation completed"
    ]
    assert completion
    assert completion[-1]["client_operation_id"] == "file-archive-test-1"
    assert completion[-1]["request_id"] == "req-archive-artifact"
    assert completion[-1]["response_source"] == "artifact"
    assert completion[-1]["outcome"] == "success"
    assert completion[-1]["status_code"] == 200


@pytest.mark.asyncio
async def test_archive_prefers_artifacts_without_live_attach_for_lost_shadow_clone_lease(monkeypatch):
    async def _preferred_lease(*_args, **_kwargs):
        return {
            "run_id": "run-lost-archive",
            "project_id": "proj-1",
            "sandbox_id": "sb-test",
            "binding_state": "lost",
        }

    attach_calls = []

    async def _get_project_sandbox(*args, **kwargs):
        attach_calls.append((args, kwargs))
        raise RuntimeError("lost lease should not attempt live attach")

    async def _artifact_paths(*args, **kwargs):
        return ["/workspace/final.txt"]

    async def _artifact_read(*args, **kwargs):
        return SimpleNamespace(content_type="text/plain"), b"artifact-final"

    monkeypatch.setattr(sandbox_api, "get_preferred_project_sandbox_lease", _preferred_lease)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_project_sandbox)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "collect_file_paths", _artifact_paths)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)

    response = await sandbox_api.download_files_archive(
        sandbox_id="sb-test",
        payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
        background_tasks=BackgroundTasks(),
        request=None,
        user_id="user-1",
    )

    assert attach_calls == []
    assert response.headers.get("x-archive-response-source") == "artifact"
    assert response.headers.get("x-archive-sandbox-available") == "0"

    archive_path = response.path
    assert archive_path
    try:
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as archive:
            assert archive.read("final.txt") == b"artifact-final"
            report = json.loads(archive.read("_download_report.json"))
            assert report["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
    finally:
        if archive_path and os.path.exists(archive_path):
            os.remove(archive_path)


@pytest.mark.asyncio
async def test_archive_stale_sandbox_id_can_canonicalize_via_artifact_hint(monkeypatch):
    requested_sandbox_ids = []

    async def _verify_access(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Sandbox not found")

    async def _artifact_project_hint(*, sandbox_ids, client=None):
        requested_sandbox_ids.extend(sandbox_ids)
        return {"project_id": "proj-1", "sandbox_id": "sb-stale"}

    async def _load_project(_client, project_id):
        assert project_id == "proj-1"
        return {
            "project_id": "proj-1",
            "account_id": "user-1",
            "sandbox": json.dumps({"id": "sb-current", "type": "desktop", "state": "running"}),
        }

    async def _unavailable_sandbox(client, *, project_id, requested_sandbox_id, **kwargs):
        assert project_id == "proj-1"
        assert requested_sandbox_id == "sb-current"
        raise RuntimeError("sandbox attach failed")

    async def _artifact_paths(*args, **kwargs):
        return ["/workspace/final.txt"]

    async def _artifact_read(*args, **kwargs):
        return SimpleNamespace(content_type="text/plain"), b"canonicalized"

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(
        sandbox_api.workspace_artifacts,
        "resolve_project_for_sandbox_hints",
        _artifact_project_hint,
    )
    monkeypatch.setattr(sandbox_api, "_load_project_by_id", _load_project)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _unavailable_sandbox)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "collect_file_paths", _artifact_paths)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)

    response = await sandbox_api.download_files_archive(
        sandbox_id="sb-stale",
        payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
        background_tasks=BackgroundTasks(),
        request=None,
        user_id="user-1",
    )

    archive_path = response.path
    assert requested_sandbox_ids == ["sb-stale"]
    try:
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as archive:
            report = json.loads(archive.read("_download_report.json"))
            assert report["sandbox_id"] == "sb-current"
            assert report["identity_source"] == "artifact_hint"
            assert archive.read("final.txt") == b"canonicalized"
    finally:
        if archive_path and os.path.exists(archive_path):
            os.remove(archive_path)


@pytest.mark.asyncio
async def test_archive_explicit_directory_path_expands_recursively(monkeypatch):
    async def _get_project_sandbox(*args, **kwargs):
        return _FakeSandbox(_FakeFiles()), "sb-explicit-dir", None

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_project_sandbox)

    async def _direct_guarded(*args, **kwargs):
        call = args[2]
        return call()

    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _direct_guarded)

    response = await sandbox_api.download_files_archive(
        sandbox_id="sb-explicit-dir",
        payload=sandbox_api.ArchiveDownloadRequest(
            paths=["/workspace/data"],
            continue_on_error=True,
        ),
        background_tasks=BackgroundTasks(),
        request=None,
        user_id="user-1",
    )

    assert response.media_type == "application/zip"
    assert response.headers.get("x-archive-total") == "1"
    assert response.headers.get("x-archive-succeeded") == "1"
    assert response.headers.get("x-archive-failed") == "0"
    assert response.headers.get("x-archive-response-source") == "sandbox"

    archive_path = response.path
    assert archive_path
    try:
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as archive:
            names = set(archive.namelist())
            assert "data/nested.md" in names

            report = json.loads(archive.read("_download_report.json"))
            assert report["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
            assert report["requested_paths"] == ["/workspace/data/nested.md"]
    finally:
        if archive_path and os.path.exists(archive_path):
            os.remove(archive_path)


@pytest.mark.asyncio
async def test_archive_missing_artifact_store_returns_deterministic_404(monkeypatch):
    async def _unavailable_sandbox(*args, **kwargs):
        raise RuntimeError("sandbox attach failed")

    async def _artifact_paths(*args, **kwargs):
        return []

    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _unavailable_sandbox)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "collect_file_paths", _artifact_paths)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "availability_state", lambda: "disabled")

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api.download_files_archive(
            sandbox_id="sb-archive",
            payload=sandbox_api.ArchiveDownloadRequest(root_path="/workspace", continue_on_error=True),
            background_tasks=BackgroundTasks(),
            request=SimpleNamespace(
                headers={
                    "x-request-id": "req-archive-empty",
                    "x-client-operation-id": "file-archive-empty",
                },
                state=SimpleNamespace(),
            ),
            user_id="user-1",
        )

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["error_code"] == "ARCHIVE_NO_FILES_FOUND"
    assert detail["artifact_store_state"] == "disabled"
    assert detail["sandbox_available"] is False
    assert exc_info.value.headers.get("X-Request-Id") == "req-archive-empty"
    assert exc_info.value.headers.get("X-Client-Operation-Id") == "file-archive-empty"
    assert exc_info.value.headers.get("X-Archive-Outcome") == "error"
    assert exc_info.value.headers.get("X-Archive-Sandbox-Available") == "0"
