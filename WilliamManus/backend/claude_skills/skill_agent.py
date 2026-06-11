"""
ADK LlmAgent + Claude Skills 动态加载机制实现
实现类似 Claude Code 使用 Skills 的三级加载系统：
  - Level 1: 元数据（始终在上下文）
  - Level 2: SKILL.md 正文（激活时加载）
  - Level 3: references/scripts/assets（按需加载）
"""

import os
import sys
import yaml
import subprocess
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# === 配置 ===
SKILLS_DIR = Path(__file__).parent
WORKSPACE = SKILLS_DIR / "workspace"
WORKSPACE.mkdir(exist_ok=True)


# =============================================================================
#  SkillRegistry - 技能注册表
# =============================================================================

class SkillRegistry:
    """
    技能注册表：扫描 claude_skills/ 目录，解析每个 SKILL.md 的元数据。
    提供元数据查询、内容加载等接口。
    """

    def __init__(self, skills_dir: Path = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.skills: dict = {}  # {name: {metadata, path}}
        self._scan_skills()

    def _scan_skills(self):
        """扫描所有 Skills 目录，解析元数据"""
        for skill_path in self.skills_dir.iterdir():
            if not skill_path.is_dir():
                continue
            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                continue

            metadata = self._parse_frontmatter(skill_md)
            if metadata and "name" in metadata:
                self.skills[metadata["name"]] = {
                    "metadata": metadata,
                    "path": skill_path
                }

    def _parse_frontmatter(self, skill_md: Path) -> Optional[dict]:
        """解析 SKILL.md 的 YAML frontmatter"""
        try:
            content = skill_md.read_text()
            if not content.startswith("---"):
                return None
            parts = content.split("---", 2)
            if len(parts) < 3:
                return None
            return yaml.safe_load(parts[1])
        except Exception as e:
            print(f"Warning: Failed to parse {skill_md}: {e}")
            return None

    def get_all_metadata(self) -> dict:
        """获取所有 Skills 的元数据（name + description）"""
        result = {}
        for name, data in self.skills.items():
            result[name] = {
                "name": data["metadata"].get("name", name),
                "description": data["metadata"].get("description", "")
            }
        return result

    def get_skill_instruction(self, skill_name: str) -> Optional[str]:
        """获取 Skill 的完整指令（SKILL.md 正文）"""
        if skill_name not in self.skills:
            return None
        skill_md = self.skills[skill_name]["path"] / "SKILL.md"
        content = skill_md.read_text()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return content

    def get_reference(self, skill_name: str, ref_name: str) -> Optional[str]:
        """获取 Skill 的参考文档"""
        if skill_name not in self.skills:
            return None
        skill_path = self.skills[skill_name]["path"]

        # 可能的参考文档位置
        candidates = [
            skill_path / ref_name,
            skill_path / "references" / ref_name,
            skill_path / "ooxml" / ref_name,
        ]

        for path in candidates:
            if path.exists() and path.is_file():
                content = path.read_text()
                # 截断过长的内容
                if len(content) > 15000:
                    return content[:15000] + "\n\n... (content truncated)"
                return content
        return None

    def list_scripts(self, skill_name: str) -> list:
        """列出 Skill 中可用的脚本"""
        if skill_name not in self.skills:
            return []
        skill_path = self.skills[skill_name]["path"]
        scripts = []

        # 搜索脚本目录
        script_dirs = [
            skill_path / "scripts",
            skill_path / "ooxml" / "scripts",
            skill_path,  # 顶层 .py 文件
        ]

        for script_dir in script_dirs:
            if script_dir.exists():
                for f in script_dir.iterdir():
                    if f.is_file() and f.suffix in [".py", ".js", ".sh"]:
                        # 排除 __init__.py 和测试文件
                        if f.name.startswith("__") or f.name.endswith("_test.py"):
                            continue
                        rel_path = f.relative_to(skill_path)
                        scripts.append({
                            "name": f.name,
                            "path": str(rel_path),
                            "full_path": str(f),
                            "type": f.suffix[1:]  # py, js, sh
                        })
        return scripts

    def list_assets(self, skill_name: str) -> list:
        """列出 Skill 中的资源文件（模板等）"""
        if skill_name not in self.skills:
            return []
        skill_path = self.skills[skill_name]["path"]
        assets = []

        # 搜索资源目录
        asset_dirs = [
            skill_path / "assets",
            skill_path / "templates",
            skill_path / "scripts" / "templates",
            skill_path / "ooxml" / "schemas",
        ]

        for asset_dir in asset_dirs:
            if asset_dir.exists() and asset_dir.is_dir():
                for f in asset_dir.rglob("*"):
                    if f.is_file():
                        rel_path = f.relative_to(skill_path)
                        assets.append({
                            "name": f.name,
                            "path": str(rel_path),
                            "full_path": str(f),
                            "size": f.stat().st_size
                        })
        return assets

    def get_script_path(self, skill_name: str, script_name: str) -> Optional[str]:
        """获取脚本的完整路径"""
        scripts = self.list_scripts(skill_name)
        for script in scripts:
            if script["name"] == script_name or script["path"] == script_name:
                return script["full_path"]
        return None


# === 全局 Registry 实例 ===
registry = SkillRegistry()


# =============================================================================
#  Skill 管理工具函数
# =============================================================================

def get_available_skills() -> dict:
    """
    Get metadata of all available Claude Skills.
    Returns the name and description of each Skill.
    The Agent should use this to decide which Skill to load based on user request.

    Returns:
        dict with skill names as keys and their metadata (name, description) as values.
    """
    return {
        "skills": registry.get_all_metadata(),
        "note": "Use load_skill(skill_name) to get detailed instructions for a specific Skill."
    }


def load_skill(skill_name: str) -> dict:
    """
    Load the full instructions (SKILL.md body) for a specific Skill.
    Call this when you need to use a Skill's capabilities.

    Args:
        skill_name: Name of the skill (pdf, xlsx, docx, or pptx).

    Returns:
        dict with 'instruction' containing the full Skill instructions,
        or 'error' if the Skill doesn't exist.
    """
    instruction = registry.get_skill_instruction(skill_name)
    if instruction is None:
        available = list(registry.skills.keys())
        return {"error": f"Skill '{skill_name}' not found. Available: {available}"}

    # 获取可用的资源信息
    scripts = registry.list_scripts(skill_name)

    return {
        "skill_name": skill_name,
        "instruction": instruction,
        "available_scripts": [s["name"] for s in scripts],
        "available_references": _get_reference_names(skill_name),
        "note": "Use load_reference() for detailed docs, run_skill_script() to execute scripts."
    }


def _get_reference_names(skill_name: str) -> list:
    """获取参考文档名称列表"""
    if skill_name not in registry.skills:
        return []
    skill_path = registry.skills[skill_name]["path"]
    refs = []

    # 常见参考文档位置
    ref_patterns = [
        ("*.md", skill_path),
        ("*.md", skill_path / "references"),
        ("*.md", skill_path / "ooxml"),
    ]

    for pattern, path in ref_patterns:
        if path.exists():
            for f in path.glob(pattern):
                if f.name != "SKILL.md":
                    refs.append(f.name)
    return list(set(refs))


def load_reference(skill_name: str, reference_name: str) -> dict:
    """
    Load a reference document from a Skill.
    Use this when you need more detailed information about a specific topic.

    Args:
        skill_name: Name of the skill.
        reference_name: Name of the reference document (e.g., "forms.md", "reference.md").

    Returns:
        dict with 'content' or 'error'.
    """
    content = registry.get_reference(skill_name, reference_name)
    if content is None:
        available = _get_reference_names(skill_name)
        return {
            "error": f"Reference '{reference_name}' not found in skill '{skill_name}'.",
            "available_references": available
        }
    return {
        "skill_name": skill_name,
        "reference_name": reference_name,
        "content": content
    }


def list_skill_scripts(skill_name: str) -> dict:
    """
    List all available scripts in a Skill.

    Args:
        skill_name: Name of the skill.

    Returns:
        dict with 'scripts' list containing script info.
    """
    if skill_name not in registry.skills:
        return {"error": f"Skill '{skill_name}' not found."}

    scripts = registry.list_scripts(skill_name)
    return {
        "skill_name": skill_name,
        "scripts": scripts,
        "note": "Use run_skill_script(skill_name, script_name, args) to execute a script."
    }


def list_skill_assets(skill_name: str) -> dict:
    """
    List all assets (templates, schemas, etc.) in a Skill.

    Args:
        skill_name: Name of the skill.

    Returns:
        dict with 'assets' list.
    """
    if skill_name not in registry.skills:
        return {"error": f"Skill '{skill_name}' not found."}

    assets = registry.list_assets(skill_name)
    return {
        "skill_name": skill_name,
        "assets": assets[:50],  # 限制数量，避免输出过长
        "total_count": len(assets)
    }


# =============================================================================
#  执行工具函数
# =============================================================================

def run_skill_script(skill_name: str, script_name: str, args: list = None) -> dict:
    """
    Execute a script from a Skill.

    Args:
        skill_name: Name of the skill.
        script_name: Name of the script (e.g., "check_fillable_fields.py").
        args: List of command-line arguments for the script.

    Returns:
        dict with 'stdout', 'stderr', 'returncode', or 'error'.
    """
    script_path = registry.get_script_path(skill_name, script_name)
    if script_path is None:
        available = registry.list_scripts(skill_name)
        return {
            "error": f"Script '{script_name}' not found in skill '{skill_name}'.",
            "available_scripts": [s["name"] for s in available]
        }

    args = args or []

    # 根据脚本类型选择执行器
    if script_path.endswith(".py"):
        cmd = [sys.executable, script_path] + [str(a) for a in args]
    elif script_path.endswith(".js"):
        cmd = ["node", script_path] + [str(a) for a in args]
    elif script_path.endswith(".sh"):
        cmd = ["bash", script_path] + [str(a) for a in args]
    else:
        return {"error": f"Unsupported script type: {script_path}"}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(WORKSPACE)
        )
        return {
            "script": script_name,
            "stdout": result.stdout[:8000] if result.stdout else "",
            "stderr": result.stderr[:2000] if result.stderr else "",
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Script execution timed out after 120 seconds"}
    except FileNotFoundError as e:
        return {"error": f"Interpreter not found: {e}"}
    except Exception as e:
        return {"error": str(e)}


def run_python_code(code: str) -> dict:
    """
    Execute Python code and return the result.
    The code runs in the workspace directory: /tmp/workspace
    Use this to run code examples from SKILL.md instructions.

    Args:
        code: Python code to execute.

    Returns:
        dict with 'stdout', 'stderr', 'returncode', and 'files_created'.
    """
    # 记录执行前的文件
    before_files = set(WORKSPACE.glob("*"))

    # 写入临时脚本
    script_path = WORKSPACE / "_temp_script.py"
    script_path.write_text(code)

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(WORKSPACE)
        )

        # 检查新创建的文件
        after_files = set(WORKSPACE.glob("*"))
        new_files = [str(f) for f in (after_files - before_files) if f.name != "_temp_script.py"]

        return {
            "stdout": result.stdout[:8000] if result.stdout else "",
            "stderr": result.stderr[:2000] if result.stderr else "",
            "returncode": result.returncode,
            "files_created": new_files,
            "workspace": str(WORKSPACE)
        }
    except subprocess.TimeoutExpired:
        return {"error": "Code execution timed out after 120 seconds"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if script_path.exists():
            script_path.unlink()


def run_shell_command(command: str) -> dict:
    """
    Run a shell command in the workspace directory.
    Use this for running tools like pandoc, npm, soffice, etc.

    Args:
        command: Shell command to execute.

    Returns:
        dict with 'stdout', 'stderr', and 'returncode'.
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=120, cwd=str(WORKSPACE)
        )
        return {
            "stdout": result.stdout[:8000] if result.stdout else "",
            "stderr": result.stderr[:2000] if result.stderr else "",
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out after 120 seconds"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
#  文件操作工具函数
# =============================================================================

def create_file(file_path: str, content: str) -> dict:
    """
    Create a text file with given content.

    Args:
        file_path: Path where the file will be created (relative to workspace or absolute).
        content: Text content to write.

    Returns:
        dict with 'success' and 'path' or 'error'.
    """
    try:
        path = Path(file_path)
        if not path.is_absolute():
            path = WORKSPACE / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {"success": True, "path": str(path)}
    except Exception as e:
        return {"error": str(e)}


def read_file(file_path: str) -> dict:
    """
    Read content from a file.

    Args:
        file_path: Path to the file (relative to workspace or absolute).

    Returns:
        dict with 'content' or 'error'.
    """
    try:
        path = Path(file_path)
        if not path.is_absolute():
            path = WORKSPACE / path
        if not path.exists():
            return {"error": f"File not found: {path}"}

        # 二进制文件只返回元信息
        if path.suffix.lower() in [".pdf", ".xlsx", ".docx", ".pptx", ".png", ".jpg"]:
            return {
                "path": str(path),
                "size": path.stat().st_size,
                "note": "Binary file - cannot display content"
            }

        content = path.read_text()
        return {"content": content[:15000], "length": len(content)}
    except Exception as e:
        return {"error": str(e)}


def list_files(directory: str = ".") -> dict:
    """
    List files in a directory.

    Args:
        directory: Path to directory (default: workspace).

    Returns:
        dict with 'files' list.
    """
    try:
        path = Path(directory)
        if not path.is_absolute():
            path = WORKSPACE / path
        if not path.exists():
            return {"error": f"Directory not found: {directory}"}
        files = []
        for item in sorted(path.iterdir()):
            if item.name.startswith("_temp"):
                continue
            files.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None
            })
        return {"files": files, "directory": str(path)}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
#  Agent 构建
# =============================================================================

def build_instruction_with_metadata() -> str:
    """构建包含 Skills 元数据的 instruction"""
    skills_metadata = registry.get_all_metadata()

    # 格式化元数据
    metadata_section = "\n".join([
        f"- **{name}**: {data['description']}"
        for name, data in skills_metadata.items()
    ])

    return f"""You are a document processing assistant with access to Claude Skills.

## Available Skills

Below are the available Skills. Read their descriptions to understand when to use each one.
When a user request matches a Skill's description, use `load_skill(skill_name)` to get detailed instructions.

{metadata_section}

## Workflow

1. **Understand the request**: Determine which Skill(s) are needed based on the descriptions above
2. **Load the Skill**: Call `load_skill(skill_name)` to get detailed instructions
3. **Load references if needed**: Call `load_reference(skill_name, ref_name)` for additional details
4. **Execute**: Follow the Skill's instructions using `run_python_code()` or `run_skill_script()`
5. **Verify**: Use `list_files()` to confirm the output was created successfully

## Workspace

All files should be created in: {WORKSPACE}
Use relative paths when creating files.

## Tool Reference

### Skill Management
- `get_available_skills()` - See all available Skills and their descriptions
- `load_skill(name)` - Load a Skill's full instructions (SKILL.md body)
- `load_reference(skill, ref)` - Load reference documentation from a Skill
- `list_skill_scripts(skill)` - List available scripts in a Skill
- `list_skill_assets(skill)` - List assets (templates, schemas) in a Skill

### Execution
- `run_skill_script(skill, script, args)` - Execute a Skill's script
- `run_python_code(code)` - Execute custom Python code
- `run_shell_command(cmd)` - Execute shell command (pandoc, npm, etc.)

### File Operations
- `create_file(path, content)` - Create a text file
- `read_file(path)` - Read file content
- `list_files(directory)` - List files in directory

## Important Notes

- Always load a Skill first before trying to create documents of that type
- The Skill instructions contain code examples - use run_python_code() to execute them
- After creating a file, always verify it was created with list_files()
- Some Skills have scripts for common operations - check list_skill_scripts() first
"""


def create_document_agent():
    """创建使用 Claude Skills 动态加载的文档处理 Agent"""
    from google.adk.agents import LlmAgent
    from google.adk.models.lite_llm import LiteLlm

    instruction = build_instruction_with_metadata()

    model = LiteLlm(
        model="openrouter/google/gemini-3-flash-preview",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        api_base=os.getenv("OPENROUTER_BASE_URL"),
    )

    # 所有工具函数
    tools = [
        # Skill 管理
        get_available_skills,
        load_skill,
        load_reference,
        list_skill_scripts,
        list_skill_assets,
        # 执行
        run_skill_script,
        run_python_code,
        run_shell_command,
        # 文件操作
        create_file,
        read_file,
        list_files,
    ]

    agent = LlmAgent(
        name="DocumentAgent",
        instruction=instruction,
        model=model,
        tools=tools
    )

    return agent


# =============================================================================
#  交互循环
# =============================================================================

async def chat_loop():
    """多轮对话主循环"""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    print("=" * 70)
    print("  ADK LlmAgent + Claude Skills (Dynamic Loading)")
    print("  Model: google/gemini-3-flash-preview via OpenRouter")
    print(f"  Workspace: {WORKSPACE}")
    print("=" * 70)

    # 显示已注册的 Skills
    skills = registry.get_all_metadata()
    print(f"\nRegistered Skills ({len(skills)}):")
    for name, data in skills.items():
        desc = data["description"][:60] + "..." if len(data["description"]) > 60 else data["description"]
        print(f"  - {name}: {desc}")

    print("\nExample requests:")
    print("  - 'Create a simple PDF with title Hello World'")
    print("  - 'Create an Excel file with a budget table'")
    print("  - 'Check if a PDF has fillable fields'")
    print("\nCommands: 'quit' to exit, 'files' to list workspace files\n")

    agent = create_document_agent()
    session_service = InMemorySessionService()

    await session_service.create_session(
        app_name="skill_agent",
        user_id="user",
        session_id="session"
    )

    runner = Runner(
        app_name="skill_agent",
        agent=agent,
        session_service=session_service
    )

    while True:
        try:
            user_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_text:
            continue
        if user_text.lower() in ['quit', 'exit', 'q']:
            print("Bye!")
            break
        if user_text.lower() == 'files':
            print(f"\nFiles in {WORKSPACE}:")
            for f in WORKSPACE.iterdir():
                if not f.name.startswith("_temp"):
                    print(f"  {f.name} ({f.stat().st_size} bytes)")
            continue

        user_input = types.Content(
            role="user",
            parts=[types.Part(text=user_text)]
        )

        print("-" * 50)

        async for event in runner.run_async(
            user_id="user",
            session_id="session",
            new_message=user_input
        ):
            if hasattr(event, 'content') and event.content:
                for part in event.content.parts:
                    if hasattr(part, 'text') and part.text:
                        print(f"\nAgent: {part.text}")
                    if hasattr(part, 'function_call') and part.function_call:
                        fc = part.function_call
                        args_str = str(dict(fc.args))[:100]
                        print(f"\n  [Tool] {fc.name}({args_str}...)")
                    if hasattr(part, 'function_response') and part.function_response:
                        resp = part.function_response.response
                        if isinstance(resp, dict):
                            if 'error' in resp:
                                print(f"  [Error] {resp['error'][:150]}")
                            elif 'files_created' in resp and resp['files_created']:
                                print(f"  [Created] {resp['files_created']}")
                            elif 'instruction' in resp:
                                print(f"  [Loaded] Skill '{resp.get('skill_name')}' instructions")
                            elif 'skills' in resp:
                                print(f"  [Skills] {list(resp['skills'].keys())}")
                            elif 'returncode' in resp:
                                status = "OK" if resp['returncode'] == 0 else f"Failed({resp['returncode']})"
                                print(f"  [Result] {status}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(chat_loop())
