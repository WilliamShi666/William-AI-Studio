# Claude Agent SDK sandbox template
# Builds on the standard code-interpreter base, adds Claude Code CLI + Python SDK.
FROM image.ppinfra.com/sandbox/code-interpreter:latest

USER root

# Global pip settings for faster installs
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_DEFAULT_TIMEOUT=300

# npm mirror
RUN npm config set registry https://registry.npmmirror.com

ENV TMPDIR=/tmp
RUN chmod 1777 /tmp

# Allow non-root global npm installs
ENV NPM_CONFIG_PREFIX=/opt/npm-global
ENV NODE_PATH=/opt/npm-global/lib/node_modules
ENV PATH=/opt/nodejs/bin:/opt/npm-global/bin:$PATH
RUN mkdir -p /opt/npm-global && chmod -R 777 /opt/npm-global && \
    npm config -g set prefix "${NPM_CONFIG_PREFIX}" && \
    echo "prefix=${NPM_CONFIG_PREFIX}" > /etc/npmrc && \
    printf 'export NPM_CONFIG_PREFIX=%s\nexport PATH=%s/bin:$PATH\n' "${NPM_CONFIG_PREFIX}" "${NPM_CONFIG_PREFIX}" > /etc/profile.d/npm-global.sh

# System dependencies (minimal — just what Claude Code + skills need)
RUN rm -f /etc/apt/sources.list.d/nodesource.list 2>/dev/null || true && \
    DEBIAN_FRONTEND=noninteractive apt-get update && \
    apt-get install -y --no-install-recommends \
      bc curl git gzip less net-tools psmisc tar unzip wget zip \
      build-essential gcc g++ make pkg-config \
      ffmpeg \
      libcairo2-dev libpango1.0-dev libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev libjpeg62-turbo libopenjp2-7 \
      texlive-latex-base texlive-latex-extra texlive-fonts-recommended latexmk dvisvgm \
      python3 python3-venv python3-dev python3-pip \
      default-jre default-jre-headless \
      poppler-utils tesseract-ocr libreoffice qpdf pandoc \
      fonts-liberation fonts-dejavu fonts-noto-cjk && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Prefer Python 3.11 when available
RUN if command -v python3.11 >/dev/null 2>&1; then \
      update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1; \
    fi
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# ── Node.js 22.x ──
ENV NODE_VERSION=22.13.0
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /opt && \
    ln -sfn "/opt/node-v${NODE_VERSION}-linux-x64" /opt/nodejs && \
    ln -sf /opt/nodejs/bin/node /usr/local/bin/node && \
    ln -sf /opt/nodejs/bin/npm /usr/local/bin/npm && \
    ln -sf /opt/nodejs/bin/npx /usr/local/bin/npx && \
    ln -sf /opt/nodejs/bin/corepack /usr/local/bin/corepack && \
    rm -f /tmp/node.tar.xz

# ── Claude Code CLI ──
# Installed globally so the Python SDK can invoke it.
RUN npm install -g @anthropic-ai/claude-code

# ── Python Claude Agent SDK ──
RUN python3 -m pip install --no-cache-dir claude-agent-sdk

# DeepSeek Anthropic-compatible endpoint defaults.
# ANTHROPIC_AUTH_TOKEN is injected at runtime via sandbox envs (never hardcoded).
ENV ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ENV ANTHROPIC_MODEL=deepseek-v4-pro[1m]
ENV ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4-pro
ENV ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-pro
ENV ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash
ENV CLAUDE_CODE_SUBAGENT_MODEL=deepseek-v4-pro
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1
ENV CLAUDE_CODE_EFFORT_LEVEL=max

# ── Claude Agent Service (runs inside sandbox) ──
COPY backend/agentscope_integration/claude_agent_service.py /opt/claude_agent_service.py

# ── Claude Skills ──
# Mounted at ~/.claude/skills/ so Claude Code auto-discovers them.
COPY backend/claude_skills/ /opt/claude_skills/
ENV CLAUDE_SKILLS_DIR=/opt/claude_skills

RUN mkdir -p /root/.claude /workspace/.claude && \
    ln -sfn /opt/claude_skills /root/.claude/skills && \
    ln -sfn /opt/claude_skills /workspace/.claude/skills

# Symlink ultimate document skills to top level (Claude Code scans one level)
RUN ln -sfn /opt/claude_skills/document_skills_ultimate/docx-ultimate /opt/claude_skills/docx-ultimate && \
    ln -sfn /opt/claude_skills/document_skills_ultimate/pdf-ultimate /opt/claude_skills/pdf-ultimate && \
    ln -sfn /opt/claude_skills/document_skills_ultimate/pptx-ultimate /opt/claude_skills/pptx-ultimate && \
    ln -sfn /opt/claude_skills/document_skills_ultimate/xlsx-ultimate /opt/claude_skills/xlsx-ultimate

# Cleanup: remove old/redundant skills and non-skill directories
RUN rm -rf /opt/claude_skills/workspace \
    /opt/claude_skills/document_skills_ultimate/*-workspace \
    /opt/claude_skills/minimax_skills \
    /opt/claude_skills/minimax_skills_backup \
    /opt/claude_skills/.claude \
    /opt/claude_skills/.playwright-mcp \
    /opt/claude_skills/document_skills_ultimate/.claude \
    /opt/claude_skills/pdf \
    /opt/claude_skills/docx \
    /opt/claude_skills/pptx \
    /opt/claude_skills/xlsx \
    /opt/claude_skills/frontend-design \
    /opt/claude_skills/hero.png \
    /opt/claude_skills/landing-page-current.png \
    /opt/claude_skills/skill-creator-ultimate.skill \
    /opt/claude_skills/marketing-mode-guide.md \
    /opt/claude_skills/SKILLS_README.md \
    /opt/claude_skills/skill_agent.py \
    /opt/claude_skills/american_civil_war_ppt \
    /opt/claude_skills/american_civil_war_ppt_v2 \
    /opt/claude_skills/american_civil_war_qwen \
    /opt/claude_skills/american_civil_war_v4 \
    /opt/claude_skills/vectors_planes_courseware \
    /opt/claude_skills/vectors_planes_v2

# ── Document skill Python deps ──
RUN python3 -m pip install --no-cache-dir \
      pypdf pdfplumber reportlab pdf2image pytesseract pypdfium2 \
      openpyxl defusedxml \
      python-pptx markitdown \
      python-dotenv dashscope google-genai \
      yfinance \
      playwright

# Playwright Chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN mkdir -p /opt/playwright-browsers && \
    npm install -g playwright && \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers npx playwright install chromium --with-deps && \
    chmod -R 777 /opt/playwright-browsers && \
    printf 'export PLAYWRIGHT_BROWSERS_PATH=%s\n' "${PLAYWRIGHT_BROWSERS_PATH}" > /etc/profile.d/playwright-browsers.sh

# ── Verify ──
RUN node --version && npm --version && \
    which claude && claude --version && \
    python3 -c "import claude_agent_sdk; print('claude_agent_sdk: OK')" && \
    python3 /opt/claude_agent_service.py --help 2>/dev/null || true && \
    ls /opt/claude_skills/pdf-ultimate/SKILL.md && echo "pdf-ultimate: OK" && \
    ls /opt/claude_skills/docx-ultimate/SKILL.md && echo "docx-ultimate: OK" && \
    ls /opt/claude_skills/pptx-ultimate/SKILL.md && echo "pptx-ultimate: OK" && \
    ls /opt/claude_skills/xlsx-ultimate/SKILL.md && echo "xlsx-ultimate: OK" && \
    ls /root/.claude/skills && echo "Skills symlink: OK" && \
    echo "Claude Agent template ready"
