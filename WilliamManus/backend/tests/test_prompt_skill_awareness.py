import importlib.util
import sys
import types
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_PROMPT_PATH = BACKEND_ROOT / "agentscope_integration" / "prompts" / "orchestrator_prompt.py"
WORKER_PROMPT_PATH = BACKEND_ROOT / "agentscope_integration" / "prompts" / "worker_prompt.py"
SKILL_AWARENESS_PATH = BACKEND_ROOT / "agentscope_integration" / "prompts" / "skill_awareness.py"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_orchestrator_prompt_includes_shared_skill_awareness() -> None:
    module = _load_module("orchestrator_prompt_skill_awareness_test", ORCHESTRATOR_PROMPT_PATH)
    prompt = module.get_orchestrator_prompt()
    assert "financial-analysis" in prompt
    assert "summarize" in prompt
    assert "ppt-generator-image-model" in prompt
    assert "frontend-design-ultimate" in prompt
    assert "pdf-resume-to-md" in prompt
    assert "ios-application-dev" in prompt
    assert "android-native-dev" in prompt
    assert "himalaya" not in prompt


def test_worker_prompt_includes_shared_skill_awareness() -> None:
    module = _load_module("worker_prompt_skill_awareness_test", WORKER_PROMPT_PATH)
    prompt = module.get_worker_prompt()
    assert "financial-analysis" in prompt
    assert "summarize" in prompt
    assert "ppt-generator-image-model" in prompt
    assert "frontend-design-ultimate" in prompt
    assert "pdf-resume-to-md" in prompt
    assert "ios-application-dev" in prompt
    assert "android-native-dev" in prompt
    assert "himalaya" not in prompt


def test_prompts_do_not_recommend_generator_scripts_for_file_rewrites() -> None:
    orch = _load_module("orchestrator_prompt_no_generator_script_test", ORCHESTRATOR_PROMPT_PATH)
    worker = _load_module("worker_prompt_no_generator_script_test", WORKER_PROMPT_PATH)
    combined = orch.get_orchestrator_prompt() + "\n" + worker.get_worker_prompt()

    assert "Python generator script" not in combined
    assert "COMPLEX LOGIC → Python Scripts" not in combined
    assert "Large rewrites (>500 lines): write a Python generator script" not in combined


def test_prompts_keep_edit_file_as_primary_repair_path() -> None:
    orch = _load_module("orchestrator_prompt_edit_primary_test", ORCHESTRATOR_PROMPT_PATH)
    worker = _load_module("worker_prompt_edit_primary_test", WORKER_PROMPT_PATH)
    combined = orch.get_orchestrator_prompt() + "\n" + worker.get_worker_prompt()

    assert "edit_file for existing files (surgical changes) ← FIRST CHOICE" in combined
    assert "Never write a helper script B whose only purpose is to" in combined


def test_prompts_include_general_document_processing_workflow_without_case_specific_rules() -> None:
    orch = _load_module("orchestrator_prompt_document_workflow_test", ORCHESTRATOR_PROMPT_PATH)
    worker = _load_module("worker_prompt_document_workflow_test", WORKER_PROMPT_PATH)
    combined = orch.get_orchestrator_prompt() + "\n" + worker.get_worker_prompt()

    assert "Document Processing Workflow" in combined
    assert "authoritative inputs" in combined
    assert "Extract just enough evidence" in combined
    assert "sampling/targeted extraction first" in combined
    assert "Do not fully transcribe or exhaustively extract" in combined
    assert "write deliverables" in combined
    assert "No web" in combined and "search unless explicitly asked" in combined
    assert "answer keys or companion solution files" in combined
    assert "Do not create exhaustive extraction" in combined
    assert "No package installs for ordinary report extraction" in combined
    assert "summarize/vision" in combined
    assert "≤100 lines" in combined
    assert "CIE" not in combined
    assert "wrong-question" not in combined
    assert "mark scheme" not in combined.lower()
    assert "NEVER use heredocs for Python code" not in combined




def test_prompts_include_tokenizer_loss_and_html_static_dynamic_guidance() -> None:
    orch = _load_module("orchestrator_prompt_html_tokenizer_guidance_test", ORCHESTRATOR_PROMPT_PATH)
    worker = _load_module("worker_prompt_html_tokenizer_guidance_test", WORKER_PROMPT_PATH)
    combined = orch.get_orchestrator_prompt() + "\n" + worker.get_worker_prompt()

    assert "Tokenizer-sensitive expression fallback" in combined
    assert "3 failed attempts" in combined
    assert "new Chart" in combined
    assert "createChart" in combined
    assert "static report layer" in combined
    assert "dynamic/interactive" in combined and "layer is progressive enhancement" in combined
    assert "progressive enhancement" in combined
    assert "If the user explicitly asks for dynamic" in combined


def test_prompts_forbid_rewrite_repair_paths_for_existing_files() -> None:
    orch = _load_module("orchestrator_prompt_no_rewrite_repair_test", ORCHESTRATOR_PROMPT_PATH)
    worker = _load_module("worker_prompt_no_rewrite_repair_test", WORKER_PROMPT_PATH)
    combined = orch.get_orchestrator_prompt() + "\n" + worker.get_worker_prompt()

    assert "write_file for new files or complete rewrites" not in combined
    assert "Re-write_file from scratch" not in combined
    assert "rewrite from scratch" not in combined.lower()
    assert "Never delete or rewrite an existing target file" in combined
    assert "sed -i only after edit_file fails" in combined
    assert "old_text and new_text MUST be different" in combined


def test_prompts_warn_about_tokenizer_sensitive_code_names() -> None:
    orch = _load_module("orchestrator_prompt_tokenizer_test", ORCHESTRATOR_PROMPT_PATH)
    worker = _load_module("worker_prompt_tokenizer_test", WORKER_PROMPT_PATH)
    combined = orch.get_orchestrator_prompt() + "\n" + worker.get_worker_prompt()

    assert "tokenizer-sensitive" in combined
    assert "summary_table" in combined
    assert "sumary" in combined
    assert "tbl, q5_tbl, q3_tbl, rows, cells" in combined
    assert "NameError: Did you mean" in combined
    assert "do not repeatedly type similar internally chosen identifiers" in combined
    assert "Exact user-required literals override tokenizer-sensitive fallbacks" in combined
    assert "Only rename or rephrase identifiers that you choose yourself" in combined
    assert "If the user asks for exact content, filenames, regexes, numbers, or code" in combined
    assert "do not write expressions with repeated adjacent letters or digits" not in combined
    assert "summary, 0455" not in combined
    assert "cannot be reliably repaired" not in combined
    assert "Reliable DOCX generation" in combined
    assert "Use the minimal DOCX template" in combined
    assert "Document(), add_heading(), add_paragraph(), add_run(), doc.save()" in combined
    assert "Do not use Word style names such as \"List Bullet\"" in combined
    assert "plain paragraphs like '- text'" in combined
    assert "Once the .docx file exists" in combined
    assert "do not write check_doc.py" in combined
    assert "no docx.oxml" in combined
    assert "qn," in combined
    assert "OxmlElement" in combined
    assert "no makeelement" in combined
    assert "no List Bullet style" in combined


def test_orchestrator_prompt_uses_general_task_contract_not_case_specific_rules() -> None:
    module = _load_module("orchestrator_prompt_task_contract_test", ORCHESTRATOR_PROMPT_PATH)
    prompt = module.get_orchestrator_prompt()

    assert "Task Contract Rule (applies to every task)" in prompt
    assert "authoritative inputs" in prompt
    assert "Completion beats exploration" in prompt
    assert "uploaded question papers" not in prompt
    assert "wrong-question list" not in prompt
    assert "mark schemes" not in prompt


def test_skill_awareness_prompt_is_compact_runtime_inventory_guidance() -> None:
    module = _load_module("orchestrator_prompt_compact_skill_awareness_test", ORCHESTRATOR_PROMPT_PATH)
    prompt = module.get_orchestrator_prompt()

    assert "call `get_available_skills()`" in prompt
    assert "load_skill(skill_name)" in prompt
    assert "## Skill Directory" not in prompt
    assert "| **pdf-ultimate**" not in prompt
    assert len(prompt) < 28000


def test_worker_prompt_is_not_bloated_by_full_skill_directory() -> None:
    module = _load_module("worker_prompt_compact_skill_awareness_test", WORKER_PROMPT_PATH)
    prompt = module.get_worker_prompt()

    assert "call `get_available_skills()`" in prompt
    assert "## Skill Directory" not in prompt
    assert "| **pdf-ultimate**" not in prompt
    assert len(prompt) < 21500


def test_orchestrator_prompt_can_disable_remote_langfuse_prompt(monkeypatch) -> None:
    monkeypatch.setenv("DISABLE_LANGFUSE_PROMPT_FETCH", "1")
    module = _load_module("orchestrator_prompt_disable_remote_test", ORCHESTRATOR_PROMPT_PATH)

    prompt, prompt_obj = module._load_orchestrator_prompt()

    assert prompt == module._FALLBACK
    assert prompt_obj is None


def test_worker_prompt_can_disable_remote_langfuse_prompt(monkeypatch) -> None:
    monkeypatch.setenv("DISABLE_LANGFUSE_PROMPT_FETCH", "1")
    module = _load_module("worker_prompt_disable_remote_test", WORKER_PROMPT_PATH)

    prompt, prompt_obj = module._load_worker_prompt()

    assert prompt == module._FALLBACK
    assert prompt_obj is None


def _install_fake_langfuse_prompt(monkeypatch, prompt_text: str) -> None:
    class _FakePrompt:
        version = "tokenizer-test"

        def compile(self):
            return prompt_text

    fake_langfuse = types.SimpleNamespace(
        get_prompt=lambda *args, **kwargs: _FakePrompt(),
    )
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = []
    langfuse_mod = types.ModuleType("services.langfuse")
    langfuse_mod.enabled = True
    langfuse_mod.langfuse = fake_langfuse
    monkeypatch.setitem(sys.modules, "services", services_pkg)
    monkeypatch.setitem(sys.modules, "services.langfuse", langfuse_mod)
    monkeypatch.delenv("DISABLE_LANGFUSE_PROMPT_FETCH", raising=False)


def test_orchestrator_remote_prompt_sanitizes_tokenizer_literal_conflicts(monkeypatch) -> None:
    _install_fake_langfuse_prompt(
        monkeypatch,
        """
tokenizer-sensitive code names:
- When writing code, do not write expressions with repeated adjacent letters or digits: do not try summary, 0455, summary_table.
- Some tokenizers may alter these; they cannot be reliably repaired by retyping; replace them with simple clear names.
- If you see summary_table → sumary_table/summery_table, do not repeatedly type similar long identifiers; rename it short.
""",
    )
    module = _load_module("orchestrator_remote_tokenizer_sanitizer_test", ORCHESTRATOR_PROMPT_PATH)

    prompt = module.get_orchestrator_prompt()

    assert "Exact user-required literals override tokenizer-sensitive fallbacks" in prompt
    assert "If the user asks for exact content, filenames, regexes, numbers, or code" in prompt
    assert "do not write expressions with repeated adjacent letters or digits" not in prompt
    assert "cannot be reliably repaired" not in prompt
    assert "summary, 0455" not in prompt


def test_worker_remote_prompt_sanitizes_tokenizer_literal_conflicts(monkeypatch) -> None:
    _install_fake_langfuse_prompt(
        monkeypatch,
        """
tokenizer-sensitive code names:
- When writing code, do not write expressions with repeated adjacent letters or digits: do not try summary, 0455, summary_table.
- Some tokenizers may alter these; they cannot be reliably repaired by retyping; replace them with simple clear names.
- If you see summary_table → sumary_table/summery_table, do not repeatedly type similar long identifiers; rename it short.
""",
    )
    module = _load_module("worker_remote_tokenizer_sanitizer_test", WORKER_PROMPT_PATH)

    prompt = module.get_worker_prompt()

    assert "Exact user-required literals override tokenizer-sensitive fallbacks" in prompt
    assert "If the user asks for exact content, filenames, regexes, numbers, or code" in prompt
    assert "do not write expressions with repeated adjacent letters or digits" not in prompt
    assert "cannot be reliably repaired" not in prompt
    assert "summary, 0455" not in prompt


def test_orchestrator_remote_prompt_adds_literal_preservation_rule(monkeypatch) -> None:
    _install_fake_langfuse_prompt(monkeypatch, "You are a concise agent prompt.")
    module = _load_module("orchestrator_remote_literal_rule_test", ORCHESTRATOR_PROMPT_PATH)

    prompt = module.get_orchestrator_prompt()

    assert "Exact user-required literals override tokenizer-sensitive fallbacks" in prompt
    assert "filenames, regexes, numbers, or code" in prompt


def test_skill_awareness_can_disable_remote_langfuse_prompt(monkeypatch) -> None:
    monkeypatch.setenv("DISABLE_LANGFUSE_PROMPT_FETCH", "1")
    module = _load_module("skill_awareness_disable_remote_test", SKILL_AWARENESS_PATH)

    prompt = module._load_skill_awareness()

    assert prompt == module._FALLBACK
    assert "get_available_skills()" in prompt


def test_compaction_preserves_agent_prompt_when_remote_skill_awareness_is_verbose() -> None:
    skill = _load_module("skill_awareness_remote_verbose_test", SKILL_AWARENESS_PATH)
    orchestrator = _load_module(
        "orchestrator_prompt_remote_verbose_test",
        ORCHESTRATOR_PROMPT_PATH,
    )

    remote_compiled = orchestrator.ORCHESTRATOR_PROMPT_RAW_TEMPLATE.replace(
        "__SKILL_AWARENESS_SECTION__",
        skill._RAW_FALLBACK,
    )
    compacted = skill.compact_agent_prompt(remote_compiled)

    assert "# Core Identity" in compacted
    assert "# ReAct Loop" in compacted
    assert "Browser Automation" in compacted
    assert "financial-analysis" in compacted
    assert "## Skill Directory" not in compacted
    assert "| **pdf-ultimate**" not in compacted
    assert len(compacted) < 29000


def test_skill_directory_table_compaction_preserves_following_markdown_tables() -> None:
    skill = _load_module("skill_awareness_table_boundary_test", SKILL_AWARENESS_PATH)
    prompt = """
Short custom skill guidance.

## Skill Directory (sandbox paths)

| Skill | Path | Use When |
|-------|------|----------|
| **pdf-ultimate** | `/workspace/skills/pdf-ultimate/` | PDF work |

# Later Section

| Column | Value |
|--------|-------|
| keep | me |
"""

    compacted = skill._compact_skill_awareness_prompt(prompt)

    assert "## Skill Directory" not in compacted
    assert "| **pdf-ultimate**" not in compacted
    assert "# Later Section" in compacted
    assert "| keep | me |" in compacted


def test_compaction_handles_legacy_langfuse_main_prompt_shape() -> None:
    skill = _load_module("skill_awareness_legacy_langfuse_test", SKILL_AWARENESS_PATH)
    prompt = """
You are the Worker of Roys Alpha.

## MULTIMODAL CONTENT POLICY (MANDATORY — SUMMARIZE CLI + VISION MODEL)
Long multimodal manual.
# Read local file as base64
```python
print("large sample")
```
### Decision Flow (summarize CLI first, DashScope fallback)
Long decision tree.

## 2.1 WORKSPACE CONFIGURATION
Workspace details.

## 2.2 SYSTEM INFORMATION
Environment details.

## WEB PAGE BUILD STRATEGY (PREFER STATIC, ALLOW COMPLEX WHEN NEEDED)
Web details.

## 3.1 TOOL SELECTION PRINCIPLES
Document processing manual.

### Claude Skills Initialization Rules (MANDATORY)
Huge skill manual.

## 4.3 WEB SEARCH & CONTENT EXTRACTION
Research details.

# ReAct Loop
Keep this loop.
"""

    compacted = skill.compact_agent_prompt(prompt)

    assert "# ReAct Loop" in compacted
    assert "summarize CLI + Vision Model" in compacted
    assert "Claude Skills" in compacted
    assert "Long multimodal manual" not in compacted
    assert "Huge skill manual" not in compacted
    assert len(compacted) < 5000
