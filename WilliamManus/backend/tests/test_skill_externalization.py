"""Tests for skill externalization: SKILL_EXPERIMENT_MODE toggle, tar creation,
and dual-mode _ensure_claude_skills_runtime_ready."""
import os
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Config toggle tests ──────────────────────────────────────────────

def test_skill_experiment_mode_defaults_true(monkeypatch):
    """When SKILL_EXPERIMENT_MODE is not set, it should default to True."""
    monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "true")
    from utils.config import Configuration
    cfg = Configuration()
    assert cfg.SKILL_EXPERIMENT_MODE is True


def test_skill_experiment_mode_false(monkeypatch):
    """When SKILL_EXPERIMENT_MODE=false, it should be False."""
    monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "false")
    from utils.config import Configuration
    cfg = Configuration()
    assert cfg.SKILL_EXPERIMENT_MODE is False


def test_skill_experiment_mode_case_insensitive(monkeypatch):
    """SKILL_EXPERIMENT_MODE should be case-insensitive."""
    monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "FALSE")
    from utils.config import Configuration
    cfg = Configuration()
    assert cfg.SKILL_EXPERIMENT_MODE is False

    monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "True")
    cfg2 = Configuration()
    assert cfg2.SKILL_EXPERIMENT_MODE is True


# ── Tar creation tests ───────────────────────────────────────────────

def test_create_skills_tar_excludes_non_skill_files():
    """_create_skills_tar should exclude workspace dirs, pngs, and other non-skill files."""
    from sandbox.tool_base import SandboxToolsBase
    import asyncio

    # Create a temp directory structure simulating claude_skills/
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "claude_skills"
        source.mkdir()

        # Create a valid skill dir (has SKILL.md)
        skill_dir = source / "canvas-design"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: canvas-design\ndescription: Art skill\n---\n# Content")

        # Create another valid skill
        skill_dir2 = source / "pdf-ultimate"
        skill_dir2.mkdir()
        (skill_dir2 / "SKILL.md").write_text("---\nname: pdf-ultimate\ndescription: PDF skill\n---\n# Content")

        # These should be excluded
        (source / "workspace").mkdir()
        (source / "__pycache__").mkdir()
        (source / "hero.png").write_text("fake png")
        (source / "landing-page-current.png").write_text("fake png")
        (source / "SKILLS_README.md").write_text("readme")
        (source / "skill_agent.py").write_text("print('hello')")
        (source / "skill-creator-ultimate.skill").write_text("skill file")
        (source / "marketing-mode-guide.md").write_text("guide")
        (source / "american_civil_war_ppt").mkdir()
        (source / "vectors_planes_v2").mkdir()
        (source / "minimax_skills_backup").mkdir()
        (source / ".claude").mkdir()
        # Workspace suffix dirs
        (source / "docx-ultimate-workspace").mkdir()

        tar_path = SandboxToolsBase._create_skills_tar(source)
        assert tar_path.exists()
        assert tar_path.suffix == ".gz" or tar_path.name.endswith(".tar.gz")

        # Verify tar contents
        with tarfile.open(tar_path, "r:gz") as tar:
            names = tar.getnames()

        # Should include the 2 valid skills
        assert "canvas-design" in names
        assert "pdf-ultimate" in names

        # Should NOT include excluded files
        assert "workspace" not in names
        assert "__pycache__" not in names
        assert "hero.png" not in names
        assert "landing-page-current.png" not in names
        assert "SKILLS_README.md" not in names
        assert "skill_agent.py" not in names
        assert "skill-creator-ultimate.skill" not in names
        assert "marketing-mode-guide.md" not in names
        assert "american_civil_war_ppt" not in names
        assert "vectors_planes_v2" not in names
        assert "minimax_skills_backup" not in names
        assert ".claude" not in names
        assert "docx-ultimate-workspace" not in names

        # Cleanup
        tar_path.unlink(missing_ok=True)


def test_create_skills_tar_includes_all_23_skills():
    """_create_skills_tar should include the complete set of discoverable skills."""
    from sandbox.tool_base import SandboxToolsBase
    import asyncio

    # Use the actual claude_skills directory
    skills_source = (
        Path(__file__).resolve().parents[1] / "claude_skills"
    )

    if not skills_source.exists():
        pytest.skip("claude_skills directory not found")

    tar_path = SandboxToolsBase._create_skills_tar(skills_source)
    with tarfile.open(tar_path, "r:gz") as tar:
        names = set(tar.getnames())

    # Direct skills (should be present)
    assert "canvas-design" in names
    assert "financial-analysis" in names
    assert "frontend-design" in names
    assert "image-generator" in names
    assert "remotion" in names
    assert "summarize" in names
    assert "pdf_resume_to_md" in names
    assert "ppt-generator-image-model" in names
    assert "skill-creator-ultimate" in names

    # Ultimate document skills (should be present)
    assert "document_skills_ultimate" in names

    # MiniMax skills (should be present)
    assert "minimax_skills" in names

    # Excluded items
    assert "workspace" not in names
    assert "SKILLS_README.md" not in names
    assert "skill_agent.py" not in names
    assert "hero.png" not in names

    tar_path.unlink(missing_ok=True)


# ── Dual-mode branching tests ────────────────────────────────────────

class TestEnsureClaudeSkillsRuntimeReady:
    """Tests for the branching logic in _ensure_claude_skills_runtime_ready."""

    @pytest.fixture
    def mock_tool_base(self):
        """Create a mock SandboxToolsBase with just enough to test the branching."""
        base = MagicMock()
        base._shadow_clone_manifest_requires_claude_skills = MagicMock(return_value=True)
        base._sandbox_path_exists = AsyncMock(return_value=False)
        base._local_claude_skills_bootstrap_path = MagicMock(
            return_value=Path("/fake/bootstrap.py")
        )
        base._ensure_claude_skills_from_local = AsyncMock()
        base.sandbox = MagicMock()
        base._run_blocking_sandbox_call = AsyncMock()
        base._persist_workspace_artifact = AsyncMock()
        return base

    @pytest.mark.asyncio
    async def test_experiment_mode_routes_to_local(self, mock_tool_base, monkeypatch):
        """When SKILL_EXPERIMENT_MODE=true, should call _ensure_claude_skills_from_local
        even when the manifest gate would normally block (shadow_clone_mode=off)."""
        monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "true")
        from utils.config import Configuration
        cfg = Configuration()
        monkeypatch.setattr("sandbox.tool_base.config", cfg, raising=True)

        from sandbox.tool_base import SandboxToolsBase
        original = SandboxToolsBase._ensure_claude_skills_runtime_ready
        bound = original.__get__(mock_tool_base, SandboxToolsBase)

        mock_tool_base._shadow_clone_manifest_requires_claude_skills = MagicMock(return_value=False)

        with patch.object(
            SandboxToolsBase, "_ensure_claude_skills_from_local", mock_tool_base._ensure_claude_skills_from_local
        ):
            await bound(force=False, lease={"environment_manifest": {}})

        mock_tool_base._ensure_claude_skills_from_local.assert_called_once_with(force=False)

    @pytest.mark.asyncio
    async def test_experiment_mode_skips_when_skills_exist(self, mock_tool_base, monkeypatch):
        """When skills are already present in sandbox, should skip upload even with force=True."""
        monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "true")
        from utils.config import Configuration
        cfg = Configuration()
        monkeypatch.setattr("sandbox.tool_base.config", cfg, raising=True)

        mock_tool_base._sandbox_path_exists = AsyncMock(return_value=True)
        # Track if _create_skills_tar was called (it shouldn't be)
        from sandbox.tool_base import SandboxToolsBase
        original = SandboxToolsBase._ensure_claude_skills_from_local

        # We need to test the real implementation with mocked dependencies
        bound = original.__get__(mock_tool_base, SandboxToolsBase)

        with patch.object(SandboxToolsBase, "_create_skills_tar") as mock_tar:
            await bound(force=True)

        # Should have checked existence and returned early
        mock_tar.assert_not_called()
        assert mock_tool_base._sandbox_path_exists.call_count >= 2

    @pytest.mark.asyncio
    async def test_production_mode_uses_template_path(self, mock_tool_base, monkeypatch):
        """When SKILL_EXPERIMENT_MODE=false, should use existing template bootstrap path."""
        monkeypatch.setenv("SKILL_EXPERIMENT_MODE", "false")
        from utils.config import Configuration
        cfg = Configuration()
        monkeypatch.setattr("sandbox.tool_base.config", cfg, raising=True)

        # Mock experiment mode to ensure it's NOT called
        mock_tool_base._ensure_claude_skills_from_local = AsyncMock()

        # Create real bootstrap file
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
        tmp.write(b"#!/usr/bin/env python3\nprint('ok')\n")
        tmp.close()
        mock_tool_base._local_claude_skills_bootstrap_path = MagicMock(
            return_value=Path(tmp.name)
        )

        # _sandbox_path_exists: first call returns False (the initial
        # skills_workspace_dir check short-circuits the AND — only 1 call),
        # all subsequent calls return True.
        exists_call = [0]

        async def exists_side_effect(path, expect_dir=False):
            exists_call[0] += 1
            return exists_call[0] > 1

        mock_tool_base._sandbox_path_exists = AsyncMock(
            side_effect=exists_side_effect
        )

        FakeResult = type("FakeResult", (), {"exit_code": 0, "stdout": "", "stderr": ""})
        mock_tool_base._run_blocking_sandbox_call = AsyncMock(return_value=FakeResult)
        # Production path also calls _persist_workspace_artifact (already AsyncMock from fixture)

        from sandbox.tool_base import SandboxToolsBase
        original = SandboxToolsBase._ensure_claude_skills_runtime_ready
        bound = original.__get__(mock_tool_base, SandboxToolsBase)

        await bound(force=True, lease={"environment_manifest": {"prepared_by": "shadow_clone_preconfirm_prepare"}})

        # Should NOT have called the experiment-mode method
        mock_tool_base._ensure_claude_skills_from_local.assert_not_called()
        # Should have used the template-bootstrap path via _run_blocking_sandbox_call
        assert mock_tool_base._run_blocking_sandbox_call.called
        os.unlink(tmp.name)
