"""skill_awareness section — pulled from Langfuse with hardcoded fallback.

Eager-loaded at import time: orchestrator/worker prompts 在 module top-level
执行 `.replace("__SKILL_AWARENESS_SECTION__", SKILL_AWARENESS_SECTION)`,所以
本模块必须在 import 期就把字符串准备好。

UI 改完 prompt 后,worker 需要重启才能拉新版（v2 SDK 在 import 期固化字符串,
不会在运行时自动刷新）。完整的 60s 缓存 + 自动刷新留待 Track A Phase 2 重构
为 lazy/composability 引用时再做。
"""
from __future__ import annotations

import re
import os

from utils.logger import logger

_SKILL_DIRECTORY_TABLE_RE = re.compile(
    r"\n## Skill Directory \(sandbox paths\)\n\n"
    r"\| Skill \| Path \| Use When \|\n"
    r"\|[-|]+\|\n"
    r"(?:\|[^\n]*\|\n)+",
)
_OLD_RUNTIME_NOTE_RE = re.compile(
    r"\nDo not assume every directory under `/opt/claude_skills` belongs in the "
    r"proactive shortlist\..*?runtime confirmation\.\n?",
    re.DOTALL,
)
_MULTIMODAL_SECTION_RE = re.compile(
    r"\n# Multimodal Content Policy \(Summarize CLI \+ Vision Model\)\n"
    r".*?"
    r"\n# Execution Environment\n",
    re.DOTALL,
)
_ORCHESTRATOR_SPECIAL_CAPABILITIES_RE = re.compile(
    r"\n## Special Capabilities\n"
    r".*?"
    r"(?=\n### Claude Skills — Mandatory Protocol\n)",
    re.DOTALL,
)
_ORCHESTRATOR_SKILLS_PROTOCOL_RE = re.compile(
    r"\n### Claude Skills — Mandatory Protocol\n"
    r".*?"
    r"(?=\n(?:__SKILL_AWARENESS_SECTION__|Use the Claude Skills toolbox proactively|Use Claude Skills when))",
    re.DOTALL,
)
_WORKER_NETLIFY_SECTION_RE = re.compile(
    r"\n## Netlify Deployment\n"
    r".*?"
    r"(?=\n# Claude Skills\n)",
    re.DOTALL,
)
_WORKER_SKILLS_PROTOCOL_RE = re.compile(
    r"\n# Claude Skills\n"
    r".*?"
    r"(?=\n(?:__SKILL_AWARENESS_SECTION__|Use the Claude Skills toolbox proactively|Use Claude Skills when|# File Editing Strategy))",
    re.DOTALL,
)
_WORKER_DEEP_RESEARCH_RE = re.compile(
    r"\n# Deep Research Execution Mode \(MECE\)\n"
    r".*?"
    r"(?=\n# Output Format\n)",
    re.DOTALL,
)
_VERBOSE_SKILL_AWARENESS_BLOCK_RE = re.compile(
    r"Use the Claude Skills toolbox proactively whenever the task matches a specialist workflow"
    r".*?"
    r"Do not assume every directory under `/opt/claude_skills` belongs in the proactive "
    r"shortlist\..*?runtime confirmation\.",
    re.DOTALL,
)
_LEGACY_MULTIMODAL_SECTION_RE = re.compile(
    r"\n## MULTIMODAL CONTENT POLICY \(MANDATORY — SUMMARIZE CLI \+ VISION MODEL\)\n"
    r".*?"
    r"(?=\n## 2\.1 WORKSPACE CONFIGURATION\n)",
    re.DOTALL,
)
_LEGACY_WORKSPACE_SECTION_RE = re.compile(
    r"\n## 2\.1 WORKSPACE CONFIGURATION\n"
    r".*?"
    r"(?=\n## 2\.2 SYSTEM INFORMATION\n)",
    re.DOTALL,
)
_LEGACY_SYSTEM_SECTION_RE = re.compile(
    r"\n## 2\.2 SYSTEM INFORMATION\n"
    r".*?"
    r"(?=\n## WEB PAGE BUILD STRATEGY)",
    re.DOTALL,
)
_LEGACY_WEB_BUILD_SECTION_RE = re.compile(
    r"\n## WEB PAGE BUILD STRATEGY \(PREFER STATIC, ALLOW COMPLEX WHEN NEEDED\)\n"
    r".*?"
    r"(?=\n(?:### MANIM|## 3\.1 TOOL SELECTION PRINCIPLES|## 4\.4 TOOL SELECTION PRINCIPLES|### Claude Skills Initialization Rules|# File Editing|# ReAct Loop))",
    re.DOTALL,
)
_LEGACY_MANIM_SECTION_RE = re.compile(
    r"\n### MANIM \(Mathematical Animations\)\n"
    r".*?"
    r"(?=\n### REMOTION \(Programmatic Video Creation\)\n)",
    re.DOTALL,
)
_LEGACY_REMOTION_SECTION_RE = re.compile(
    r"\n### REMOTION \(Programmatic Video Creation\)\n"
    r".*?"
    r"(?=\n(?:### FRONTEND DESIGN|### NETLIFY DEPLOYMENT|### Claude Skills Initialization Rules|## 3\.1 TOOL SELECTION PRINCIPLES))",
    re.DOTALL,
)
_LEGACY_FRONTEND_DESIGN_SECTION_RE = re.compile(
    r"\n### FRONTEND DESIGN \(Visual Design for Web, Slides, PPT, Courseware, and More\)\n"
    r".*?"
    r"(?=\n### NETLIFY DEPLOYMENT\n)",
    re.DOTALL,
)
_LEGACY_NETLIFY_SECTION_RE = re.compile(
    r"\n### NETLIFY DEPLOYMENT\n"
    r".*?"
    r"(?=\n(?:### CLAUDE SKILLS|### Claude Skills Initialization Rules|## 3\.1 TOOL SELECTION PRINCIPLES|# File Editing))",
    re.DOTALL,
)
_LEGACY_CLAUDE_SKILLS_SECTION_RE = re.compile(
    r"\n### (?:CLAUDE SKILLS \(Document Processing\) - MANDATORY PROTOCOL|Claude Skills Initialization Rules \(MANDATORY\))\n"
    r".*?"
    r"(?=\n(?:## 3\.1 TOOL SELECTION PRINCIPLES|## 4\.3 WEB SEARCH|## 4\.4 TOOL SELECTION PRINCIPLES|# File Editing|# ReAct Loop))",
    re.DOTALL,
)
_LEGACY_TOOL_SELECTION_SECTION_RE = re.compile(
    r"\n## (?:3\.1|4\.4) TOOL SELECTION PRINCIPLES\n"
    r".*?"
    r"(?=\n(?:## 4\.3 WEB SEARCH|# File Editing|# Task Routing|# ReAct Loop))",
    re.DOTALL,
)
_LEGACY_WEB_SEARCH_SECTION_RE = re.compile(
    r"\n## 4\.3 WEB SEARCH & CONTENT EXTRACTION\n"
    r".*?"
    r"(?=\n(?:## 5\.1 WRITING GUIDELINES|# Data Integrity|# Content Creation|# ReAct Loop|# Output Format))",
    re.DOTALL,
)
_LEGACY_WRITING_OUTPUT_SECTION_RE = re.compile(
    r"\n## 5\.1 WRITING GUIDELINES\n"
    r".*?"
    r"(?=\n# 6\. COMMUNICATION & USER INTERACTION\n)",
    re.DOTALL,
)
_LEGACY_COMMUNICATION_SECTION_RE = re.compile(
    r"\n# 6\. COMMUNICATION & USER INTERACTION\n"
    r".*?"
    r"(?=\n# 7\. WEB DEPLOYMENT\n)",
    re.DOTALL,
)
_LEGACY_WEB_DEPLOYMENT_SECTION_RE = re.compile(
    r"\n# 7\. WEB DEPLOYMENT\n"
    r".*?"
    r"(?=\n# 8\. COMPUTER USE & DESKTOP AUTOMATION\n)",
    re.DOTALL,
)
_LEGACY_COMPUTER_USE_SECTION_RE = re.compile(
    r"\n# 8\. COMPUTER USE & DESKTOP AUTOMATION\n"
    r".*?"
    r"(?=\n# 9\. REACT LOOP)",
    re.DOTALL,
)

_SKILL_AWARENESS_RUNTIME_NOTE = (
    "Use runtime inventory (`get_available_skills()`, `load_skill(skill_name)`) "
    "to confirm active skills; keep optional skills on-demand."
)
_COMPACT_SKILL_AWARENESS = """
Use Claude Skills for specialist workflows. Call `get_available_skills()`, then
`load_skill(skill_name)`, and prefer `run_skill_script()` when scripts exist.
Common skills: pdf-ultimate/docx-ultimate/xlsx-ultimate/pptx-ultimate,
ppt-generator-image-model, summarize, image-generator, frontend-design,
frontend-design-ultimate-1.0.0, manim-animation, remotion,
remotion-best-practices-1.0.0, financial-analysis, canvas-design,
marketing-mode-1.0.0, find-skills-0.1.0, android-native-dev, ios-application-dev,
pdf-resume-to-md.
"""
_COMPACT_MULTIMODAL_SECTION = """
# Multimodal Content Policy (Summarize CLI + Vision Model)

- For images, screenshots, charts, scanned PDFs, audio, video, or garbled extraction,
  use summarize CLI first, then DashScope vision fallback if summarize fails.
- DashScope env vars are pre-injected: DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL,
  DASHSCOPE_VISION_MODEL. Never print or hardcode secrets.
- For text PDFs/documents, try text extraction first; if empty/garbled, switch to
  summarize/vision. Treat <vision-grounding> as authoritative visual facts.

# Execution Environment
"""
_COMPACT_LEGACY_MULTIMODAL_SECTION = """
## MULTIMODAL CONTENT POLICY (summarize CLI + Vision Model)

- For visual, scanned, audio, or video content, use summarize first and DashScope
  vision fallback when needed. Env vars are pre-injected; never print secrets.
- For text documents, try text extraction first; switch to summarize/vision if
  extraction is empty or garbled.
"""
_COMPACT_LEGACY_WORKSPACE_SECTION = """
## 2.1 WORKSPACE CONFIGURATION

- Operate in /workspace by default. Save all user-downloadable deliverables under
  /workspace and report those paths clearly.
"""
_COMPACT_LEGACY_SYSTEM_SECTION = """
## 2.2 SYSTEM INFORMATION

- Python 3.11, Node.js/npm, Chromium, common document/data/visualization tools, and
  Claude Skills dependencies are available in the sandbox.
"""
_COMPACT_LEGACY_WEB_BUILD_SECTION = """
## WEB PAGE BUILD STRATEGY

- Prefer static HTML/CSS/JS with CDN assets. Use npm/Vite only when explicitly
  required or genuinely necessary; if build fails/OOM, fall back to static output.
"""
_COMPACT_LEGACY_SPECIAL_CAPABILITY = """
### SPECIALIST CAPABILITY ROUTER

- Manim: load_skill("manim-animation") before coding; use CJK-safe Text for Chinese.
- Remotion: load best-practices then remotion skill; scaffold/render via scripts.
- Frontend design: load frontend-design for visual/layout work; add
  frontend-design-ultimate-1.0.0 for React/Tailwind/shadcn interfaces.
- Netlify: deploy only on explicit request for a public URL; never print tokens.
"""
_COMPACT_LEGACY_CLAUDE_SKILLS = """
### Claude Skills Protocol

For specialist workflows, call `get_available_skills()`, then
`load_skill(skill_name)`, then follow the loaded skill. Prefer `run_skill_script()`
when scripts exist. For PPT, default to ppt-generator-image-model unless editable
PPTX is requested; editable PPTX uses pptx-ultimate/html2pptx plus frontend-design.
Never create slides with python-pptx except screenshot-image insertion fallback.
"""
_COMPACT_LEGACY_TOOL_SELECTION = """
## TOOL SELECTION PRINCIPLES

- Document processing: use Claude Skills first.
- General operations: use focused CLI tools.
- Existing files: edit target files directly with edit_file; avoid helper repair
  scripts and heredocs.
"""
_COMPACT_LEGACY_WEB_SEARCH = """
## WEB SEARCH & CONTENT EXTRACTION

Use web_search first, scrape_webpage for full source text when needed, and browser
automation only for interaction or pages that scraping cannot handle.
"""
_COMPACT_LEGACY_WRITING_OUTPUT = """
## WRITING & FILE OUTPUT

Write substantive prose when requested; use concise lists when appropriate. For large
outputs, create one primary /workspace artifact and edit it throughout. Deep research
may create report, sources, and evidence artifacts.
"""
_COMPACT_LEGACY_COMMUNICATION = """
# COMMUNICATION & USER INTERACTION

Save all user-viewable files under /workspace and report paths clearly. Keep status
updates honest and concise.
"""
_COMPACT_LEGACY_WEB_DEPLOYMENT = """
# WEB DEPLOYMENT

Do not deploy by default. Use Netlify only when explicitly asked for a public URL or
external sharing; check credential presence only and never print secrets.
"""
_COMPACT_LEGACY_COMPUTER_USE = """
# COMPUTER USE & DESKTOP AUTOMATION

For GUI/browser interaction, take a screenshot first, then click/type/scroll/wait and
verify with another screenshot. Use Chromium in the sandbox.
"""
_COMPACT_ORCHESTRATOR_SPECIAL_CAPABILITIES = """
## Special Capabilities (load detailed instructions on demand)

- Manim: before writing animation code, load_skill("manim-animation"). Use
  Text("中文", font="Noto Sans CJK SC") for Chinese and edit the scene directly
  for fixes; never create helper repair scripts.
- Remotion: load_skill("remotion-best-practices-1.0.0") then load_skill("remotion");
  scaffold/render via run_skill_script and save final video under /workspace.
- Netlify: deploy only when the user explicitly asks for a public URL or external
  sharing. Check env-var presence only; never print tokens.

"""
_COMPACT_ORCHESTRATOR_SKILLS_PROTOCOL = """
### Claude Skills — On-Demand Protocol

When a specialist workflow matches, use the skill system instead of embedding every
recipe in the system prompt:
1. call `get_available_skills()`;
2. call `load_skill(skill_name)` to load authoritative instructions;
3. execute with `run_skill_script()` or the loaded skill's direct-tool workflow.

For PPT: default to ppt-generator-image-model unless editable PPTX is explicitly
requested; editable PPTX uses pptx-ultimate/html2pptx plus frontend-design. Never
create slides with python-pptx except screenshot-image insertion fallback.
"""
_COMPACT_WORKER_NETLIFY_SECTION = """
## Netlify Deployment

Do not deploy by default. Use Netlify only when the task explicitly asks for a
public URL or external sharing; verify credential presence only and never print
tokens.
"""
_COMPACT_WORKER_SKILLS_PROTOCOL = """
# Claude Skills

For PDF/XLSX/DOCX/PPTX and other specialist workflows:
1. call `get_available_skills()` to confirm availability;
2. call `load_skill(skill_name)` for authoritative instructions;
3. execute with `run_skill_script()` or the loaded skill's direct-tool workflow.

PPT defaults to ppt-generator-image-model unless editable PPTX is requested.
Editable PPTX uses pptx-ultimate/html2pptx plus frontend-design. Never create slides
with python-pptx except screenshot-image insertion fallback. For Manim, Remotion,
documents, and verification, load the matching skill before implementation and save
final outputs under /workspace. Common skills include summarize, financial-analysis,
frontend-design-ultimate, pdf-resume-to-md, android-native-dev, and ios-application-dev.
"""
_COMPACT_WORKER_DEEP_RESEARCH = """
# Deep Research Execution Mode (MECE)

When the Orchestrator delegates research, follow its task description, knowledge
gaps, working plan, language, and artifact requirements exactly. Work depth-first,
use citations for factual claims, scrape key sources instead of relying on snippets,
and produce the requested /workspace report/source/evidence artifacts. If a plan step
fails twice, switch method and report the failure signature plus what should change.
"""


def _compact_skill_awareness_prompt(text: str) -> str:
    """Remove the static directory table and keep a short runtime guidance note."""
    if text.lstrip().startswith("Use the Claude Skills toolbox proactively"):
        return _COMPACT_SKILL_AWARENESS.strip() + "\n"

    compacted = _VERBOSE_SKILL_AWARENESS_BLOCK_RE.sub(
        _COMPACT_SKILL_AWARENESS.strip(),
        text,
    )

    if "## Skill Directory (sandbox paths)" not in compacted:
        return compacted

    compacted = _SKILL_DIRECTORY_TABLE_RE.sub(
        "\n\n" + _SKILL_AWARENESS_RUNTIME_NOTE + "\n",
        compacted,
        count=1,
    )
    compacted = _OLD_RUNTIME_NOTE_RE.sub("", compacted)
    return compacted.strip() + "\n"


def compact_agent_prompt(text: str) -> str:
    """Compact high-noise embedded manuals while preserving capability routers."""
    compacted = _compact_skill_awareness_prompt(text)
    compacted = _MULTIMODAL_SECTION_RE.sub(_COMPACT_MULTIMODAL_SECTION, compacted)
    compacted = _ORCHESTRATOR_SPECIAL_CAPABILITIES_RE.sub(
        _COMPACT_ORCHESTRATOR_SPECIAL_CAPABILITIES,
        compacted,
    )
    compacted = _ORCHESTRATOR_SKILLS_PROTOCOL_RE.sub(
        _COMPACT_ORCHESTRATOR_SKILLS_PROTOCOL,
        compacted,
    )
    compacted = _WORKER_NETLIFY_SECTION_RE.sub(_COMPACT_WORKER_NETLIFY_SECTION, compacted)
    compacted = _WORKER_SKILLS_PROTOCOL_RE.sub(_COMPACT_WORKER_SKILLS_PROTOCOL, compacted)
    compacted = _WORKER_DEEP_RESEARCH_RE.sub(_COMPACT_WORKER_DEEP_RESEARCH, compacted)
    compacted = _LEGACY_MULTIMODAL_SECTION_RE.sub(
        _COMPACT_LEGACY_MULTIMODAL_SECTION,
        compacted,
    )
    compacted = _LEGACY_WORKSPACE_SECTION_RE.sub(
        _COMPACT_LEGACY_WORKSPACE_SECTION,
        compacted,
    )
    compacted = _LEGACY_SYSTEM_SECTION_RE.sub(_COMPACT_LEGACY_SYSTEM_SECTION, compacted)
    compacted = _LEGACY_WEB_BUILD_SECTION_RE.sub(
        _COMPACT_LEGACY_WEB_BUILD_SECTION,
        compacted,
    )
    compacted = _LEGACY_MANIM_SECTION_RE.sub(
        _COMPACT_LEGACY_SPECIAL_CAPABILITY,
        compacted,
    )
    compacted = _LEGACY_REMOTION_SECTION_RE.sub("", compacted)
    compacted = _LEGACY_FRONTEND_DESIGN_SECTION_RE.sub("", compacted)
    compacted = _LEGACY_NETLIFY_SECTION_RE.sub("", compacted)
    compacted = _LEGACY_CLAUDE_SKILLS_SECTION_RE.sub(
        _COMPACT_LEGACY_CLAUDE_SKILLS,
        compacted,
    )
    compacted = _LEGACY_TOOL_SELECTION_SECTION_RE.sub(
        _COMPACT_LEGACY_TOOL_SELECTION,
        compacted,
    )
    compacted = _LEGACY_WEB_SEARCH_SECTION_RE.sub(_COMPACT_LEGACY_WEB_SEARCH, compacted)
    compacted = _LEGACY_WRITING_OUTPUT_SECTION_RE.sub(
        _COMPACT_LEGACY_WRITING_OUTPUT,
        compacted,
    )
    compacted = _LEGACY_COMMUNICATION_SECTION_RE.sub(
        _COMPACT_LEGACY_COMMUNICATION,
        compacted,
    )
    compacted = _LEGACY_WEB_DEPLOYMENT_SECTION_RE.sub(
        _COMPACT_LEGACY_WEB_DEPLOYMENT,
        compacted,
    )
    compacted = _LEGACY_COMPUTER_USE_SECTION_RE.sub(
        _COMPACT_LEGACY_COMPUTER_USE,
        compacted,
    )
    return compacted.strip() + "\n"


# Hardcoded fallback —— 保留高价值短名单即可；静态目录表会拖慢 DeepSeek。
# 内容在 Langfuse UI `skill-awareness` v1 应该一致，但本地会再做 runtime compaction。
_RAW_FALLBACK = """
Use the Claude Skills toolbox proactively whenever the task matches a specialist workflow instead of reinventing it with ad hoc code. The authoritative runtime inventory lives in `/workspace/skills/` and `/workspace/skills/skills_metadata.json`; call `get_available_skills()` to confirm what is active in the sandbox, then `load_skill(skill_name)` before execution. Prefer `run_skill_script()` when a skill exposes scripts, and treat bundled references/templates as authoritative.

Proactively consider these high-value skills when they match the request:
- **pdf-ultimate**, **docx-ultimate**, **xlsx-ultimate**, **pptx-ultimate**: advanced document creation, editing, validation, and manipulation with multi-toolchain support (Python, JavaScript, OOXML XML, design-system pipelines). These replace the older pdf/docx/xlsx/pptx skills.
- **canvas-design**: visual layout playbooks for HTML canvas-based designs
- **financial-analysis**: DCF, comps, LBO, 3-statement, equity research, investment banking, private equity, and wealth-management workflows with yfinance-backed data gathering
- **summarize**: fast URL/local-file/PDF/image/audio/YouTube summarization and briefing workflows
- **image-generator**: single-image generation with DashScope/Qwen image models
- **ppt-generator-image-model** (DEFAULT for PPT/slides): AI-powered slide generation using qwen-image-2.0-pro; produces high-quality 16:9 slide images in gradient-glass or vector-illustration styles. Use as first choice for any PPT/presentation task unless user explicitly requests editable PPTX
- **frontend-design**: sandbox-safe static HTML/CSS/JS interface design (lightweight)
- **frontend-design-ultimate-1.0.0**: higher-end React/Tailwind/shadcn UI blueprints when the task explicitly calls for that stack
- **manim-animation**: mathematical animation creation with Manim Community Edition — code templates, CJK/LaTeX rules, render workflow
- **remotion** and **remotion-best-practices-1.0.0**: scaffolded React video generation plus specialist Remotion rules
- **marketing-mode-1.0.0**: marketing strategy, positioning, SEO, CRO, paid growth, and messaging frameworks
- **find-skills-0.1.0**: discover whether an installable skill already exists before building bespoke tooling
- **android-native-dev**: Android native application development guidance with Material Design 3, Kotlin/Compose standards, and build troubleshooting (reference-only, SDK not pre-installed)
- **ios-application-dev**: iOS native app development guidance covering UIKit, SnapKit, SwiftUI, Apple HIG, and accessibility (reference-only, Xcode not pre-installed)
- **pdf-resume-to-md**: HR/recruiting skill — converts resume PDFs to structured Markdown for candidate analysis, comparison dashboards, and bulk resume processing

## Skill Directory (sandbox paths)

| Skill | Path | Use When |
|-------|------|----------|
| **pdf-ultimate** | `/workspace/skills/pdf-ultimate/` | PDF creation, extraction, forms, merge/split, OCR, watermark |
| **docx-ultimate** | `/workspace/skills/docx-ultimate/` | Word doc creation, editing, redlining, templates, CJK typography |
| **xlsx-ultimate** | `/workspace/skills/xlsx-ultimate/` | Spreadsheet creation, editing, formula recalc, financial models |
| **pptx-ultimate** | `/workspace/skills/pptx-ultimate/` | Editable PPTX creation via html2pptx.js, Visual Strategy Analysis |
| **ppt-generator-image-model** | `/workspace/skills/ppt-generator-image-model/` | **DEFAULT for PPT/slides**: AI image generation (qwen-image-2.0), gradient-glass/vector styles |
| **canvas-design** | `/workspace/skills/canvas-design/` | Visual layout playbooks, HTML canvas designs |
| **financial-analysis** | `/workspace/skills/financial-analysis/` | DCF, comps, LBO, 3-statement, equity research, IB, PE, wealth management |
| **summarize** | `/workspace/skills/summarize/` | URL/file/PDF/image/audio/YouTube summarization |
| **image-generator** | `/workspace/skills/image-generator/` | Single image generation with DashScope/Qwen |
| **frontend-design** | `/workspace/skills/frontend-design/` | Static HTML/CSS/JS interface design (lightweight) |
| **frontend-design-ultimate-1.0.0** | `/workspace/skills/frontend-design-ultimate-1.0.0/` | React/Tailwind/shadcn UI blueprints |
| **manim-animation** | `/workspace/skills/manim-animation/` | Manim math animation — code templates, CJK/LaTeX rules, render pipeline |
| **remotion** | `/workspace/skills/remotion/` | React video generation |
| **remotion-best-practices-1.0.0** | `/workspace/skills/remotion-best-practices-1.0.0/` | Remotion specialist rules |
| **marketing-mode-1.0.0** | `/workspace/skills/marketing-mode-1.0.0/` | Marketing strategy, SEO, CRO, paid growth |
| **find-skills-0.1.0** | `/workspace/skills/find-skills-0.1.0/` | Discover installable skills |
| **android-native-dev** | `/workspace/skills/android-native-dev/` | Android dev guidance: Material Design 3, Kotlin/Compose (reference-only) |
| **ios-application-dev** | `/workspace/skills/ios-application-dev/` | iOS dev guidance: UIKit, SwiftUI, Apple HIG (reference-only) |
| **pdf-resume-to-md** | `/workspace/skills/pdf_resume_to_md/` | Convert resume PDFs to structured Markdown |
| **skill-creator-ultimate** | `/workspace/skills/skill-creator-ultimate/` | Create new skills |
| **skill-creator-pro-2.0.0** | `/workspace/skills/skill-creator-pro-2.0.0/` | Skill creation (pro) |
| **skill-creator-0.1.0** | `/workspace/skills/skill-creator-0.1.0/` | Skill creation (basic) |
| **self-improving-agent-3.0.6** | `/workspace/skills/self-improving-agent-3.0.6/` | Self-improvement patterns |
"""

_FALLBACK = _compact_skill_awareness_prompt(_RAW_FALLBACK)


def _load_skill_awareness() -> str:
    """Pull `skill-awareness` from Langfuse; on any failure return _FALLBACK."""
    if os.getenv("DISABLE_LANGFUSE_PROMPT_FETCH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.info("[skill_awareness] Langfuse prompt fetch disabled; using fallback")
        return _FALLBACK

    try:
        # Imported lazily so a broken services.langfuse import never crashes the
        # orchestrator/worker prompts at module load time.
        from services.langfuse import enabled, langfuse
    except Exception as e:
        logger.warning("[skill_awareness] services.langfuse import failed (%s); using fallback", e)
        return _FALLBACK

    if not enabled:
        logger.info("[skill_awareness] Langfuse disabled (no keys); using fallback")
        return _FALLBACK

    try:
        prompt = langfuse.get_prompt("skill-awareness", fallback=_FALLBACK)
        # v2 SDK: TextPromptClient.compile() with no vars returns the raw text.
        # When fallback hits, the SDK still wraps it in a TextPromptClient so
        # .compile() behaves the same.
        compiled = prompt.compile() if hasattr(prompt, "compile") else str(prompt)
        compiled = _compact_skill_awareness_prompt(compiled)
        # Strip the restrictive run_skill_script guidance that discourages
        # agents from using skills for complex scripts. We WANT agents to
        # use run_skill_script for all skill-based workflows including Manim.
        compiled = compiled.replace(
            "When executing external scripts or APIs requiring complex arguments or environment configuration Avoid using run_skill_script for complex arguments due to parsing limitations; instead use execute_command with direct script paths. Explicitly export required environment variables (e.g., DASHSCOPE_API_KEY_SLOT) before execution. Validate parameter types strictly (e.g., pass JSON objects not strings) to prevent attribute errors like 'str' object has no attribute 'items'.",
            "Prefer `run_skill_script` for all skill-based workflows. It handles argument passing and environment setup correctly. Use `execute_command` only for ad-hoc commands, not for running skill scripts.",
        )
        version = getattr(prompt, "version", "fallback")
        logger.info("[skill_awareness] Loaded from Langfuse (version=%s, chars=%d)",
                    version, len(compiled))
        return compiled
    except Exception as e:
        logger.warning("[skill_awareness] get_prompt failed (%s); using fallback", e)
        return _FALLBACK


SKILL_AWARENESS_SECTION = _load_skill_awareness()
