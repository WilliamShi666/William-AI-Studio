from pathlib import Path


def test_full_claude_template_extends_complete_agent_stack():
    dockerfile = Path('/tmp/ppio-claude-full-build/ppio-claude-full.Dockerfile')
    if not dockerfile.exists():
        dockerfile = Path(__file__).resolve().parents[2] / 'ppio-claude-full.Dockerfile'
    text = dockerfile.read_text(encoding='utf-8')

    retained_full_stack_markers = [
        'manim',
        '@remotion/cli@4',
        'netlify-cli',
        'frontend-design-ultimate-1.0.0',
        'summarize --help',
        'xgboost',
    ]
    claude_markers = [
        '@anthropic-ai/claude-code',
        'claude-agent-sdk',
        'COPY backend/agentscope_integration/claude_agent_service.py /opt/claude_agent_service.py',
        '/home/user/.claude/skills',
        'CLAUDE_CODE_SUBAGENT_MODEL',
    ]

    for marker in retained_full_stack_markers + claude_markers:
        assert marker in text
