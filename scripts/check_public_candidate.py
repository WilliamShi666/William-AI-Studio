#!/usr/bin/env python3
"""High-signal public export hygiene checks.

This script is intentionally conservative. It is meant to run against the clean
public source candidate before formal git operations and in CI after the public
repository exists.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


TEXT_EXTENSIONS = {
    ".c",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".dockerfile",
    ".env",
    ".example",
    ".gitignore",
    ".gitattributes",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".lock",
    ".md",
    ".mjs",
    ".py",
    ".sample",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

DENIED_DIR_NAMES = {
    ".git",
    ".gitnexus",
    ".next",
    ".pytest_cache",
    ".turbo",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "logs",
    "node_modules",
    "playwright-report",
    "release",
    "test-results",
}

DENIED_PATH_PARTS = {
    (".playwright-mcp",),
    ("research_reports",),
    ("cc-flow-src", "release"),
    ("WilliamManus".lower(), "agent_eval"),
    ("WilliamManus".lower(), "agent_eval_articles"),
    ("WilliamManus".lower(), "backend", "screenshots"),
    ("WilliamManus".lower(), "backend", "claude_skills", "document_skills_ultimate", "docx-ultimate-workspace"),
    ("WilliamManus".lower(), "backend", "claude_skills", "document_skills_ultimate", "pdf-ultimate-workspace"),
    ("WilliamManus".lower(), "backend", "claude_skills", "document_skills_ultimate", "pptx-ultimate-workspace"),
    ("WilliamManus".lower(), "backend", "claude_skills", "document_skills_ultimate", "xlsx-ultimate-workspace"),
    ("WilliamManus".lower(), "backend", "claude_skills", "graphify-out"),
    ("WilliamManus".lower(), "backend", "claude_skills", "minimax_skills_backup"),
    ("WilliamManus".lower(), "backend", "claude_skills", "workspace"),
    ("multimodalrag", "backend", "data"),
    ("multimodalrag", "backend", "Database", "milvus_server", "data", "volumes"),
    ("multimodalrag", "backend", "output"),
    ("opensource_prep_reports",),
}

DENIED_FILE_SUFFIXES = {
    ".log",
    ".pyc",
    ".tsbuildinfo",
    ".tmp",
}

ROOT_DENIED_FILE_NAMES = {
    "ADMIN_SYSTEM_PLAN.md",
    "AGENTS.md",
    "API_STREAMING_FLOW_ANALYSIS.md",
    "CLAUDE.md",
    "DEEPSEEK_STABILITY_REPAIR_PLAN_BATCHED.md",
    "FIRECRAWL_API_GUIDE.md",
    "FUSION_GUIDE.md",
    "INFRASTRUCTURE_ANALYSIS_SUMMARY.md",
    "INTEGRATION_PLAN.md",
    "LANGFUSE_INTEGRATION_GUIDE.md",
    "LANGFUSE_INTEGRATION_PLAN.md",
    "NAVIGATION_GUIDE.md",
    "NGINX_FUSION_DEPLOYMENT.md",
    "POST_RUN_MEMORY_REVIEW_EXECUTION_PLAN.md",
    "PPIO_SANDBOX_INTEGRATION_ANALYSIS.md",
    "QUESTION_SOLVER_PLAN.md",
    "SANDBOX_TOOL_EXECUTION_FLOW.md",
    "SHADOW_CLONE_LANGFUSE_INSTRUMENTATION.md",
    "STARTUP_GUIDE.md",
    "deepseek_adaptation_review_synthesis.md",
    "deepseek_codewhale_review_notes.md",
    "deepseek_reasonix_review_notes.md",
    "deepseek_research_report_audit.md",
    "wm-backend-log.txt",
    "network_requests_round1.json",
    "research_codewhale_deepseek_adaptation.md",
    "research_codewhale_deepseek_adaptation_zh.md",
    "research_cross_comparison_synthesis.md",
    "research_cross_comparison_synthesis_zh.md",
    "research_reasonix_deepseek_adaptation.md",
    "research_reasonix_deepseek_adaptation_zh.md",
    "start_fusion.sh",
}

DENIED_FILE_NAMES = {
    "wm-backend-log.txt",
    "network_requests_round1.json",
}

DENIED_GLOBS = {
    ".excalidraw": "Excalidraw canvases require separate review; keep VibeCoding Markdown only for the first public source candidate.",
}

ALLOWED_MARKDOWN_PATHS = {
    (".github", "pull_request_template.md"),
    ("ARCHITECTURE.md",),
    ("CODE_OF_CONDUCT.md",),
    ("CONTRIBUTING.md",),
    ("GOVERNANCE.md",),
    ("README.md",),
    ("ROADMAP.md",),
    ("SECURITY.md",),
    ("SUPPORT.md",),
    ("THIRD_PARTY_NOTICES.md",),
    ("cc-flow-src", "README.md"),
    ("multimodalrag", "STARTUP_GUIDE.md"),
    ("multimodalrag", "backend", "Database", "milvus_server", "README.md"),
    ("multimodalrag", "backend", "Information-Extraction", "unified", "quick_start.md"),
    ("multimodalrag", "backend", "chat", "chat_api_doc.md"),
    ("multimodalrag", "datasets", "public_tutoring_demo", "README.md"),
}

DATASET_ASSET_EXTENSIONS = {
    ".jsonl",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}

CLAUDE_SKILLS_GENERATED_EXTENSIONS = {
    ".docx",
    ".pdf",
    ".pptx",
    ".xlsx",
}

FIXED_PRIVATE_IP_RE = re.compile(r"\b(?:117\.50\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")
LOCAL_ABS_PATH_RE = re.compile(r"/home/" + r"ubuntu(?:/|\b)")
LOCAL_DEV_BIND_ALL_RE = re.compile(
    r"""(?:os\.getenv\([^)\n]+["']0\.0\.0\.0["']|HOST\s*=\s*["']0\.0\.0\.0["']|host:\s*["']0\.0\.0\.0["']|host\s*=\s*["']0\.0\.0\.0["']|--host 0\.0\.0\.0|--hostname 0\.0\.0\.0|default=["']0\.0\.0\.0["'])"""
)
LOCAL_DEV_BIND_CHECK_PATHS = {
    ("WilliamManus".lower(), "backend", "api.py"),
    ("WilliamManus".lower(), "frontend", "ecosystem.config.cjs"),
    ("cc-flow-src", "server", "src", "index.ts"),
    ("cc-flow-src", "client", "vite.config.ts"),
    ("multimodalrag", "STARTUP_GUIDE.md"),
    ("multimodalrag", "backend", ".env.example"),
    ("multimodalrag", "backend", "start_all_services.sh"),
    ("multimodalrag", "backend", "Text_segmentation", "markdown_chunker_api.py"),
    ("multimodalrag", "backend", "Information-Extraction", "unified", "unified_pdf_extraction_service.py"),
    ("multimodalrag", "backend", "Information-Extraction", "unified", "quick_start.md"),
    ("multimodalrag", "backend", "Information-Extraction", "deepseekocr", "api_server_mineru_format.py"),
    ("multimodalrag", "backend", "Information-Extraction", "paddleocr", "api_paddleocr_vl_mineru.py"),
    ("multimodalrag", "backend", "Information-Extraction", "paddleocr", "run_paddleocr.sh"),
    ("multimodalrag", "backend", "Information-Extraction", "paddleocr", "start_paddleocr_vl.sh"),
    ("multimodalrag", "backend", "Database", "milvus_server", "milvus_api.py"),
    ("multimodalrag", "backend", "chat", "kb_chat.py"),
    ("multimodalrag", "backend", "chat", "chat_api_doc.md"),
    ("multimodalrag", "backend", "knowledge-management", "main.py"),
}
MAX_NORMAL_FILE_BYTES = 25 * 1024 * 1024
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"


def rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    return path.relative_to(root).parts


def has_path_parts(parts: tuple[str, ...], denied: tuple[str, ...]) -> bool:
    size = len(denied)
    return any(parts[index : index + size] == denied for index in range(len(parts) - size + 1))


def is_env_example(path: Path) -> bool:
    name = path.name
    return name.endswith(".example") or name.endswith(".sample") or ".example." in name


def is_probably_text(path: Path) -> bool:
    if path.name in {".gitignore", ".gitattributes", ".gitleaks.toml"}:
        return True
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    return any(path.name.endswith(suffix) for suffix in (".env.example", ".env.sample"))


def is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def has_shebang(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def add_error(errors: list[str], root: Path, path: Path, message: str) -> None:
    errors.append(f"{path.relative_to(root)}: {message}")


def check_path(root: Path, path: Path, allow_dataset_assets: bool, errors: list[str]) -> None:
    parts = rel_parts(path, root)
    lowered_parts = tuple(part.lower() for part in parts)

    for denied in DENIED_PATH_PARTS:
        if has_path_parts(lowered_parts, denied):
            add_error(errors, root, path, f"denied path contains {'/'.join(denied)}")
            return

    if (
        has_path_parts(lowered_parts, ("williammanus", "backend", "claude_skills"))
        and {"images", "merged", "complete"} & set(lowered_parts)
    ):
        add_error(errors, root, path, "denied generated claude_skills output path")
        return

    if any(part in DENIED_DIR_NAMES for part in lowered_parts[:-1]):
        add_error(errors, root, path, "file is inside a denied generated/runtime directory")
        return

    name = path.name
    suffix = path.suffix.lower()

    if len(parts) == 1 and name in ROOT_DENIED_FILE_NAMES:
        add_error(errors, root, path, "denied root-level private/rewrite file")

    if name in DENIED_FILE_NAMES:
        add_error(errors, root, path, "denied runtime/capture file name")

    if suffix in DENIED_FILE_SUFFIXES or any(name.endswith(suffix) for suffix in (".log", ".log.*", ".tmp")):
        add_error(errors, root, path, "denied runtime/cache file suffix")

    if suffix in DENIED_GLOBS:
        add_error(errors, root, path, DENIED_GLOBS[suffix])

    if suffix == ".md" and parts not in ALLOWED_MARKDOWN_PATHS:
        add_error(errors, root, path, "Markdown file is not in the first-release public Markdown allowlist")

    if name.startswith(".env") and not is_env_example(path):
        add_error(errors, root, path, "non-example env file")

    in_dataset = parts[:3] == ("multimodalrag", "datasets", "public_tutoring_demo")
    if in_dataset and suffix in DATASET_ASSET_EXTENSIONS and not allow_dataset_assets:
        add_error(errors, root, path, "dataset asset present in source-only candidate; use --allow-dataset-assets only after Git LFS/release packaging is approved")

    in_claude_skills = lowered_parts[:3] == ("williammanus", "backend", "claude_skills")
    if in_claude_skills and suffix in CLAUDE_SKILLS_GENERATED_EXTENSIONS:
        add_error(errors, root, path, "generated claude_skills document artifact should not be in the first public source candidate")

    try:
        size = path.stat().st_size
    except OSError as exc:
        add_error(errors, root, path, f"cannot stat file: {exc}")
        return

    if size > MAX_NORMAL_FILE_BYTES and not is_lfs_pointer(path):
        add_error(errors, root, path, f"large non-LFS file is {size} bytes")

    if path.stat().st_mode & 0o111 and not has_shebang(path):
        add_error(errors, root, path, "executable bit is set but file has no shebang")

    if is_probably_text(path):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            add_error(errors, root, path, f"cannot read text file: {exc}")
            return
        if FIXED_PRIVATE_IP_RE.search(text):
            add_error(errors, root, path, "fixed private/server IP pattern found")
        if LOCAL_ABS_PATH_RE.search(text):
            add_error(errors, root, path, "local server absolute path found")
        if parts in LOCAL_DEV_BIND_CHECK_PATHS and LOCAL_DEV_BIND_ALL_RE.search(text):
            add_error(errors, root, path, "local developer service should default to localhost/127.0.0.1, not 0.0.0.0")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a public source candidate for denied open-source artifacts.")
    parser.add_argument("root", nargs="?", default=".", help="candidate root, default: current directory")
    parser.add_argument(
        "--allow-dataset-assets",
        action="store_true",
        help="allow dataset PDFs/images/chunks after Git LFS or release-asset packaging is explicitly approved",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"candidate root does not exist: {root}", file=sys.stderr)
        return 2

    errors: list[str] = []
    checked = 0

    for current_root, dir_names, file_names in os.walk(root):
        current = Path(current_root)
        relative_parts = rel_parts(current, root) if current != root else ()
        lowered_relative = tuple(part.lower() for part in relative_parts)

        dir_names[:] = [
            dir_name
            for dir_name in dir_names
            if dir_name != ".git" and not has_path_parts(lowered_relative + (dir_name.lower(),), ("multimodalrag", "backend", "Database", "milvus_server", "data", "volumes"))
        ]

        for file_name in file_names:
            checked += 1
            check_path(root, current / file_name, args.allow_dataset_assets, errors)

    if errors:
        print(f"Public candidate hygiene failed: {len(errors)} issue(s) in {checked} checked file(s).", file=sys.stderr)
        for error in errors[:200]:
            print(f"- {error}", file=sys.stderr)
        if len(errors) > 200:
            print(f"... {len(errors) - 200} more issue(s) omitted", file=sys.stderr)
        return 1

    print(f"Public candidate hygiene passed: {checked} file(s) checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
