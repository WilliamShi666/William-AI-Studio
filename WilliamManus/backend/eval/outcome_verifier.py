from __future__ import annotations

from dataclasses import dataclass, field
import csv
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from typing import Any, Iterable, Protocol


@dataclass(frozen=True)
class CommandExecutionResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class OutcomeVerificationRequest:
    task_id: str
    project_id: str
    success_criteria: list[dict[str, Any]]
    thread_id: str | None = None
    agent_run_id: str | None = None
    sandbox_id: str | None = None
    required_sandbox_type: str | None = None
    artifact_root: str | None = None


@dataclass(frozen=True)
class OutcomeCheckResult:
    criterion_type: str
    passed: bool
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutcomeVerificationResult:
    passed: bool
    checks: list[OutcomeCheckResult]
    summary: str
    artifacts_found: list[str]
    artifacts_missing: list[str]
    errors: list[str]


class OutcomeVerifierIO(Protocol):
    async def read_bytes(self, path: str) -> bytes | None: ...

    async def list_dir(self, path: str) -> list[dict[str, Any]]: ...

    async def run_command(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CommandExecutionResult: ...


class _SelectorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: set[str] = set()
        self.attributes: dict[str, set[str]] = {}
        self.link_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        attrs_dict = {name: value for name, value in attrs}
        if "id" in attrs_dict and attrs_dict["id"]:
            self.ids.add(str(attrs_dict["id"]))
        for name, value in attrs_dict.items():
            self.attributes.setdefault(name, set()).add(
                "" if value is None else str(value)
            )
        if tag == "a" and attrs_dict.get("href"):
            self.link_count += 1


def _decode_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


async def _read_required_bytes(io_adapter: OutcomeVerifierIO, path: str) -> bytes:
    payload = await io_adapter.read_bytes(path)
    if payload is None:
        raise FileNotFoundError(path)
    return payload


def _load_zip_entry(data: bytes, entry: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        return archive.read(entry)


def _zip_entry_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        return archive.namelist()


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(_decode_text(data))))


def _html_collector(data: bytes) -> _SelectorCollector:
    collector = _SelectorCollector()
    collector.feed(_decode_text(data))
    return collector


def _match_selector(collector: _SelectorCollector, selector: str) -> bool:
    trimmed = selector.strip()
    if not trimmed:
        return False
    if trimmed.startswith("#"):
        return trimmed[1:] in collector.ids
    if trimmed.startswith("[") and trimmed.endswith("]"):
        attr_name = trimmed[1:-1].split("=", 1)[0].strip()
        return attr_name in collector.attributes
    if "," in trimmed:
        return any(_match_selector(collector, part) for part in trimmed.split(","))
    return trimmed in collector.tags


def _append_artifact(
    *,
    path: str | None,
    passed: bool,
    artifacts_found: set[str],
    artifacts_missing: set[str],
) -> None:
    if not path:
        return
    if passed:
        artifacts_found.add(path)
        artifacts_missing.discard(path)
    else:
        if path not in artifacts_found:
            artifacts_missing.add(path)


async def _evaluate_single_criterion(
    criterion: dict[str, Any],
    io_adapter: OutcomeVerifierIO,
) -> OutcomeCheckResult:
    criterion_type = str(criterion.get("type") or "")
    path = criterion.get("path")

    if criterion_type == "file_exists":
        payload = await io_adapter.read_bytes(str(path))
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=payload is not None,
            message="file exists" if payload is not None else "file missing",
            path=str(path),
        )

    if criterion_type == "directory_exists":
        try:
            await io_adapter.list_dir(str(path))
            return OutcomeCheckResult(
                criterion_type=criterion_type,
                passed=True,
                message="directory exists",
                path=str(path),
            )
        except Exception as exc:
            return OutcomeCheckResult(
                criterion_type=criterion_type,
                passed=False,
                message=f"directory missing: {exc}",
                path=str(path),
            )

    if criterion_type == "text_contains":
        data = await _read_required_bytes(io_adapter, str(path))
        text = _decode_text(data)
        contains = [str(item) for item in criterion.get("contains") or []]
        missing = [item for item in contains if item not in text]
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=not missing,
            message=(
                "all expected text found" if not missing else f"missing text: {missing}"
            ),
            path=str(path),
            details={"missing": missing},
        )

    if criterion_type == "text_equals":
        data = await _read_required_bytes(io_adapter, str(path))
        text = _decode_text(data)
        expected = str(criterion.get("value", ""))
        passed = text == expected
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=passed,
            message=(
                "text exactly matched expected value"
                if passed
                else "text did not exactly match expected value"
            ),
            path=str(path),
            details={
                "actual_length": len(text),
                "expected_length": len(expected),
            },
        )

    if criterion_type == "text_matches_regex":
        data = await _read_required_bytes(io_adapter, str(path))
        pattern = str(criterion.get("pattern") or "")
        matched = re.search(pattern, _decode_text(data)) is not None
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=matched,
            message="regex matched" if matched else "regex did not match",
            path=str(path),
            details={"pattern": pattern},
        )

    if criterion_type == "json_has_keys":
        data = await _read_required_bytes(io_adapter, str(path))
        payload = json.loads(_decode_text(data))
        keys = [str(item) for item in criterion.get("keys") or []]
        missing = [key for key in keys if key not in payload]
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=not missing,
            message=(
                "all json keys present"
                if not missing
                else f"missing json keys: {missing}"
            ),
            path=str(path),
            details={"missing": missing},
        )

    if criterion_type == "zip_entry_exists":
        data = await _read_required_bytes(io_adapter, str(path))
        entry = str(criterion.get("entry") or "")
        names = set(_zip_entry_names(data))
        passed = entry in names
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=passed,
            message="zip entry exists" if passed else f"zip entry missing: {entry}",
            path=str(path),
            details={"entry": entry},
        )

    if criterion_type == "zip_entry_contains":
        data = await _read_required_bytes(io_adapter, str(path))
        entry = str(criterion.get("entry") or "")
        entry_payload = _decode_text(_load_zip_entry(data, entry))
        contains = [str(item) for item in criterion.get("contains") or []]
        missing = [item for item in contains if item not in entry_payload]
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=not missing,
            message=(
                "zip entry contains expected text"
                if not missing
                else f"missing zip entry text: {missing}"
            ),
            path=str(path),
            details={"entry": entry, "missing": missing},
        )

    if criterion_type == "zip_entry_count_at_least":
        data = await _read_required_bytes(io_adapter, str(path))
        prefix = str(criterion.get("prefix") or "")
        minimum = int(criterion.get("minimum") or 0)
        count = sum(1 for name in _zip_entry_names(data) if name.startswith(prefix))
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=count >= minimum,
            message=(
                f"zip entry count {count} >= {minimum}"
                if count >= minimum
                else f"zip entry count {count} < {minimum}"
            ),
            path=str(path),
            details={"prefix": prefix, "count": count, "minimum": minimum},
        )

    if criterion_type == "html_contains_selectors":
        data = await _read_required_bytes(io_adapter, str(path))
        collector = _html_collector(data)
        selectors = [str(item) for item in criterion.get("selectors") or []]
        missing = [
            selector
            for selector in selectors
            if not _match_selector(collector, selector)
        ]
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=not missing,
            message=(
                "all selectors found"
                if not missing
                else f"missing selectors: {missing}"
            ),
            path=str(path),
            details={"missing": missing},
        )

    if criterion_type == "html_contains_text":
        data = await _read_required_bytes(io_adapter, str(path))
        text = _decode_text(data)
        contains = [str(item) for item in criterion.get("contains") or []]
        missing = [item for item in contains if item not in text]
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=not missing,
            message=(
                "html contains expected text"
                if not missing
                else f"missing html text: {missing}"
            ),
            path=str(path),
            details={"missing": missing},
        )

    if criterion_type == "html_contains_links":
        data = await _read_required_bytes(io_adapter, str(path))
        minimum_links = int(criterion.get("minimum_links") or 0)
        collector = _html_collector(data)
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=collector.link_count >= minimum_links,
            message=(
                f"link count {collector.link_count} >= {minimum_links}"
                if collector.link_count >= minimum_links
                else f"link count {collector.link_count} < {minimum_links}"
            ),
            path=str(path),
            details={
                "link_count": collector.link_count,
                "minimum_links": minimum_links,
            },
        )

    if criterion_type == "csv_row_count_at_least":
        data = await _read_required_bytes(io_adapter, str(path))
        minimum_rows = int(criterion.get("minimum_rows") or 0)
        rows = _csv_rows(data)
        count = max(len(rows) - 1, 0) if rows else 0
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=count >= minimum_rows,
            message=(
                f"csv row count {count} >= {minimum_rows}"
                if count >= minimum_rows
                else f"csv row count {count} < {minimum_rows}"
            ),
            path=str(path),
            details={"row_count": count, "minimum_rows": minimum_rows},
        )

    if criterion_type == "csv_header_contains":
        data = await _read_required_bytes(io_adapter, str(path))
        rows = _csv_rows(data)
        header = rows[0] if rows else []
        required = [str(item) for item in criterion.get("contains") or []]
        normalized_header = {column.strip().lower() for column in header}
        missing = [
            item for item in required if item.strip().lower() not in normalized_header
        ]
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=not missing,
            message=(
                "csv header contains expected fields"
                if not missing
                else f"missing csv headers: {missing}"
            ),
            path=str(path),
            details={"missing": missing},
        )

    if criterion_type == "file_size_at_least":
        data = await _read_required_bytes(io_adapter, str(path))
        minimum_bytes = int(criterion.get("minimum_bytes") or 0)
        actual = len(data)
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=actual >= minimum_bytes,
            message=(
                f"file size {actual} >= {minimum_bytes}"
                if actual >= minimum_bytes
                else f"file size {actual} < {minimum_bytes}"
            ),
            path=str(path),
            details={"size_bytes": actual, "minimum_bytes": minimum_bytes},
        )

    if criterion_type == "command_exit_zero":
        argv = [str(item) for item in criterion.get("argv") or []]
        cwd = criterion.get("cwd")
        command_result = await io_adapter.run_command(
            argv, cwd=None if cwd is None else str(cwd)
        )
        passed = int(command_result.exit_code) == 0
        return OutcomeCheckResult(
            criterion_type=criterion_type,
            passed=passed,
            message=(
                "command exited zero"
                if passed
                else f"command failed with exit code {command_result.exit_code}"
            ),
            path=None,
            details={
                "argv": argv,
                "cwd": cwd,
                "exit_code": command_result.exit_code,
                "stdout": command_result.stdout,
                "stderr": command_result.stderr,
            },
        )

    raise ValueError(f"Unsupported criterion type: {criterion_type}")


async def verify_outcome(
    request: OutcomeVerificationRequest,
    io_adapter: OutcomeVerifierIO,
) -> OutcomeVerificationResult:
    checks: list[OutcomeCheckResult] = []
    artifacts_found: set[str] = set()
    artifacts_missing: set[str] = set()
    errors: list[str] = []

    for criterion in request.success_criteria:
        criterion_type = str(criterion.get("type") or "")
        path = str(criterion.get("path") or "") or None
        try:
            result = await _evaluate_single_criterion(criterion, io_adapter)
        except FileNotFoundError:
            result = OutcomeCheckResult(
                criterion_type=criterion_type,
                passed=False,
                message="artifact missing",
                path=path,
            )
        except Exception as exc:
            result = OutcomeCheckResult(
                criterion_type=criterion_type,
                passed=False,
                message=f"verification error: {exc}",
                path=path,
            )
            errors.append(f"{criterion_type}: {exc}")

        checks.append(result)
        _append_artifact(
            path=result.path,
            passed=result.passed,
            artifacts_found=artifacts_found,
            artifacts_missing=artifacts_missing,
        )
        if not result.passed and result.message not in errors:
            errors.append(result.message)

    passed = all(check.passed for check in checks)
    summary = (
        f"{sum(1 for check in checks if check.passed)}/{len(checks)} checks passed"
    )
    return OutcomeVerificationResult(
        passed=passed,
        checks=checks,
        summary=summary,
        artifacts_found=sorted(artifacts_found),
        artifacts_missing=sorted(artifacts_missing),
        errors=errors,
    )
