# Base sandbox image with Python and common runtimes
FROM image.ppinfra.com/sandbox/code-interpreter:latest

# Switch to root user for installation
USER root

# Global pip settings for faster installs
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_DEFAULT_TIMEOUT=300

# npm 使用淘宝镜像加速
RUN npm config set registry https://registry.npmmirror.com

# Ensure /tmp is writable for headless document conversions
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

# Switch apt sources to Tsinghua mirrors for faster installs (handle alternate sources.list path)
# Use chmod to fix permissions if needed
RUN if [ -f /etc/apt/sources.list ]; then \
        chmod 644 /etc/apt/sources.list 2>/dev/null || true; \
        sed -i 's@http://deb.debian.org/debian@https://mirrors.tuna.tsinghua.edu.cn/debian@g' /etc/apt/sources.list; \
        sed -i 's@http://security.debian.org/debian-security@https://mirrors.tuna.tsinghua.edu.cn/debian-security@g' /etc/apt/sources.list; \
    fi; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        chmod 644 /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i 's@http://deb.debian.org/debian@https://mirrors.tuna.tsinghua.edu.cn/debian@g' /etc/apt/sources.list.d/debian.sources; \
        sed -i 's@http://security.debian.org/debian-security@https://mirrors.tuna.tsinghua.edu.cn/debian-security@g' /etc/apt/sources.list.d/debian.sources; \
    fi

# System dependencies for zip/unzip, media, build tools, LaTeX, GUI, and scientific stack
RUN rm -f /etc/apt/sources.list.d/nodesource.list 2>/dev/null || true && \
    DEBIAN_FRONTEND=noninteractive apt-get update && \
    apt-get install -y --no-install-recommends \
      bc curl git gzip less net-tools psmisc socat tar unzip wget zip \
      build-essential gcc g++ make pkg-config \
      ffmpeg pulseaudio pulseaudio-utils \
      libcairo2-dev libpango1.0-dev libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev libjpeg62-turbo libopenjp2-7 \
      texlive-latex-base texlive-latex-extra texlive-fonts-recommended latexmk dvisvgm \
      python3 python3-venv python3-dev python3-pip \
      default-jre default-jre-headless \
      xvfb xterm xauth xclip xdotool x11vnc \
      supervisor openssh-server openssh-client vim nano && \
    python3_11_candidate="$(apt-cache policy python3.11 2>/dev/null | awk '/Candidate:/ {print $2}')" && \
    if [ -n "${python3_11_candidate}" ] && [ "${python3_11_candidate}" != "(none)" ]; then \
      apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-dev; \
    fi && \
    openjdk11_candidate="$(apt-cache policy openjdk-11-jre 2>/dev/null | awk '/Candidate:/ {print $2}')" && \
    if [ -n "${openjdk11_candidate}" ] && [ "${openjdk11_candidate}" != "(none)" ]; then \
      apt-get install -y --no-install-recommends openjdk-11-jre openjdk-11-jre-headless; \
    fi && \
    if apt-cache show gh >/dev/null 2>&1; then \
      apt-get install -y --no-install-recommends gh; \
    fi && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Prefer Python 3.11 when available and keep pip toolchain current
RUN if command -v python3.11 >/dev/null 2>&1; then \
      update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1; \
    fi
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Install Node.js 22.x (requested) from official binaries
ENV NODE_VERSION=22.13.0
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /opt && \
    ln -sfn "/opt/node-v${NODE_VERSION}-linux-x64" /opt/nodejs && \
    ln -sf /opt/nodejs/bin/node /usr/local/bin/node && \
    ln -sf /opt/nodejs/bin/npm /usr/local/bin/npm && \
    ln -sf /opt/nodejs/bin/npx /usr/local/bin/npx && \
    ln -sf /opt/nodejs/bin/corepack /usr/local/bin/corepack && \
    rm -f /tmp/node.tar.xz

# ========== 依赖冲突解决核心 ==========
#
# 核心问题：numpy 2.x vs 1.x 生态系统冲突
# - manim 0.19+ 需要 numpy >= 2.1
# - spacy/thinc 8.3.x 需要 numpy >= 2.0 但 < 2.1
# - 旧版 pandas 1.5.x 与 numpy 2.x 二进制不兼容
#
# 解决方案：优先 manim + 机器学习，用 transformers/nltk 替代 spacy
# 详见：DEPENDENCY_CONFLICT_RESOLUTION_GUIDE.md

# Step 1: 清理基础镜像中可能存在的冲突包和损坏的 dist-info
RUN python3 -m pip uninstall -y numpy pandas thinc spacy e2b-charts 2>/dev/null || true && \
    for site_dir in /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.11/site-packages; do \
      if [ -d "${site_dir}" ]; then \
        find "${site_dir}" -name "-*" -type d -exec rm -rf {} + 2>/dev/null || true; \
      fi; \
    done

# Step 2: 安装 numpy 2.1+ 作为基准（manim 必需）
RUN python3 -m pip install --no-cache-dir "numpy>=2.1,<3.0"

# Step 3: 安装兼容 numpy 2.x 的 pandas 2.x
RUN python3 -m pip install --no-cache-dir "pandas>=2.2.0"

# Step 4: manim + 可视化依赖（最高优先级）
RUN python3 -m pip install --no-cache-dir \
      manim \
      pycairo cairosvg svgelements \
      pydub moviepy pillow

# Step 5: 数据科学 / 机器学习栈
RUN python3 -m pip install --no-cache-dir \
      polars pyarrow scipy scikit-learn statsmodels sympy \
      matplotlib seaborn plotly bokeh altair vega-datasets networkx \
      requests beautifulsoup4 lxml

# Step 6: XGBoost（机器学习，高优先级）
RUN python3 -m pip install --no-cache-dir xgboost

# Step 7: NLP 替代方案（代替 spacy，避免 thinc 与 manim 的 numpy 版本冲突）
# - transformers: Hugging Face 生态，功能强大
# - nltk: 轻量级经典 NLP 工具包
# - jieba: 中文分词
RUN python3 -m pip install --no-cache-dir \
      transformers tokenizers \
      nltk \
      jieba

# Step 8: OpenCV（低优先级，可选）
RUN python3 -m pip install --no-cache-dir opencv-python-headless || true

# Step 9: Web 框架 / 文档处理 / 工具包
RUN python3 -m pip install --no-cache-dir \
      fastapi flask uvicorn starlette \
      fpdf2 markdown weasyprint xhtml2pdf \
      tabulate click pydantic cryptography boto3 httpx openai \
      html5lib playwright

# ========== Claude Skills 依赖 ==========
# 用于支持 PDF/XLSX/DOCX/PPTX 处理能力
# 详见：CLAUDE_SKILLS_DEPENDENCIES.md

# Claude Skills 系统依赖
# 注意：禁用 nodesource 仓库以避免 GPG 签名过期问题
RUN rm -f /etc/apt/sources.list.d/nodesource.list 2>/dev/null || true && \
    DEBIAN_FRONTEND=noninteractive apt-get update && \
    apt-get install -y --no-install-recommends \
      poppler-utils \
      tesseract-ocr \
      libreoffice \
      qpdf \
      pandoc && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Claude Skills Python 依赖
RUN python3 -m pip install --no-cache-dir \
      pypdf pdfplumber reportlab pdf2image pytesseract \
      pypdfium2 \
      openpyxl \
      defusedxml \
      python-pptx markitdown \
      python-dotenv dashscope google-genai \
      yfinance

# Claude Skills + Frontend Node.js 依赖
RUN npm install -g \
      pdf-lib \
      docx pptxgenjs \
      react@19.2.1 react-dom@19.2.1 \
      react-icons lucide-react framer-motion \
      tailwindcss@4.1.14 next-themes class-variance-authority tailwind-merge \
      wouter@3.3.5 react-hook-form zod recharts \
      axios nanoid sonner streamdown \
      shadcn shadcn-ui \
      @steipete/summarize

# ========== Frontend Design Skill 依赖 ==========
RUN npm install -g \
      typescript \
      @types/react @types/react-dom \
      vite \
      create-vite

# pnpm/yarn (固定版本，避免 corepack 签名问题)
RUN npm install -g pnpm@10.28.1 yarn@1.22.22

# Playwright (用于 PPTX Skill 的 HTML 渲染)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN mkdir -p /opt/playwright-browsers && \
    npm install -g playwright && \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers npx playwright install chromium --with-deps && \
    chmod -R 777 /opt/playwright-browsers && \
    printf 'export PLAYWRIGHT_BROWSERS_PATH=%s\n' "${PLAYWRIGHT_BROWSERS_PATH}" > /etc/profile.d/playwright-browsers.sh

# Sharp (图像处理，用于 PPTX 渐变光栅化)
RUN npm install -g sharp

# ========== Remotion 视频创建依赖 ==========
# 用于支持 Remotion 编程式视频创建能力
RUN npm install -g \
      remotion@4 \
      @remotion/cli@4 \
      @remotion/renderer@4 \
      @remotion/bundler@4 \
      @remotion/transitions@4 \
      @remotion/media-utils@4 \
      @remotion/gif@4 \
      @remotion/google-fonts@4 \
      @remotion/fonts@4 \
      @remotion/noise@4 \
      @remotion/paths@4 \
      @remotion/shapes@4 \
      @remotion/layout-utils@4 \
      @remotion/three@4 \
      @remotion/lottie@4 \
      @remotion/zod-types@4 \
      @remotion/media@4 \
      three @react-three/fiber \
      lottie-web \
      zod@4.3.6

# Remotion 使用 Chromium 渲染帧 — 复用 Playwright 的 Chromium
# 设置环境变量让 Remotion 找到已安装的浏览器 (resolve glob at build time)
RUN CHROME_BIN=$(find /opt/playwright-browsers -name "chrome" -path "*/chrome-linux/*" -type f 2>/dev/null | head -1) && \
    if [ -n "$CHROME_BIN" ]; then \
      printf 'export REMOTION_CHROME_EXECUTABLE=%s\n' "$CHROME_BIN" > /etc/profile.d/remotion-chrome.sh; \
      echo "REMOTION_CHROME_EXECUTABLE=$CHROME_BIN" >> /etc/environment; \
    fi

# Ensure global npm prefix is writable for non-root installs/updates
RUN chmod -R 777 /opt/npm-global

# ========== 预装 Claude Skills ==========
# 将 Skills 文件（SKILL.md + 脚本 + 资源）复制到沙箱
# Agent 可通过 CLAUDE_SKILLS_DIR 环境变量找到 Skills
COPY backend/claude_skills/ /opt/claude_skills/
ENV CLAUDE_SKILLS_DIR=/opt/claude_skills

# ========== 清理非 skill 目录，减小镜像体积 ==========
RUN rm -rf /opt/claude_skills/workspace \
    /opt/claude_skills/document_skills_ultimate/docx-ultimate-workspace \
    /opt/claude_skills/document_skills_ultimate/pdf-ultimate-workspace \
    /opt/claude_skills/document_skills_ultimate/pptx-ultimate-workspace \
    /opt/claude_skills/document_skills_ultimate/xlsx-ultimate-workspace \
    /opt/claude_skills/minimax_skills_backup \
    /opt/claude_skills/.claude \
    /opt/claude_skills/.playwright-mcp \
    /opt/claude_skills/document_skills_ultimate/.claude \
    /opt/claude_skills/minimax_skills/.claude \
    /opt/claude_skills/minimax_skills/skills/.claude \
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

# ========== 移除被 ultimate 版本替代的旧 skills ==========
RUN rm -rf /opt/claude_skills/pdf \
    /opt/claude_skills/docx \
    /opt/claude_skills/pptx \
    /opt/claude_skills/xlsx

# ========== 为嵌套 skill 创建顶层符号链接 ==========
# bootstrap 脚本只扫描一层目录，需要符号链接使嵌套 skill 可被发现
RUN ln -sfn /opt/claude_skills/document_skills_ultimate/docx-ultimate /opt/claude_skills/docx-ultimate && \
    ln -sfn /opt/claude_skills/document_skills_ultimate/pdf-ultimate /opt/claude_skills/pdf-ultimate && \
    ln -sfn /opt/claude_skills/document_skills_ultimate/pptx-ultimate /opt/claude_skills/pptx-ultimate && \
    ln -sfn /opt/claude_skills/document_skills_ultimate/xlsx-ultimate /opt/claude_skills/xlsx-ultimate && \
    ln -sfn /opt/claude_skills/minimax_skills/skills/android-native-dev /opt/claude_skills/android-native-dev && \
    ln -sfn /opt/claude_skills/minimax_skills/skills/ios-application-dev /opt/claude_skills/ios-application-dev

# 预装最新 prompts 供沙箱内排查/调试
COPY backend/agentscope_integration/prompts/ /opt/agentscope_prompts/
ENV AGENTSCOPE_PROMPTS_DIR=/opt/agentscope_prompts

# 安装额外字体（用于 canvas-design Skill）
RUN rm -f /etc/apt/sources.list.d/nodesource.list 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends \
    fonts-liberation fonts-dejavu fonts-noto-cjk && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ========== Netlify 部署支持 ==========
# 用于将 Agent 创建的网页/Web App 部署到 Netlify
# 详见：NETLIFY_DEPLOYMENT_GUIDE.md

# 验证 Node.js 版本（基础镜像应已包含 >= 18）
RUN node --version && npm --version

# 全局安装 netlify-cli（预装以加速部署，避免每次 npx 下载）
RUN npm install -g netlify-cli

# 验证 netlify-cli 安装
RUN netlify --version

# ========== 验证安装 ==========
RUN python3 -c "import numpy; print(f'numpy: {numpy.__version__}')" && \
    python3 -c "import pandas; print(f'pandas: {pandas.__version__}')" && \
    python3 -c "import manim; print(f'manim: {manim.__version__}')" && \
    python3 -c "import xgboost; print(f'xgboost: {xgboost.__version__}')" && \
    python3 -c "import transformers; print(f'transformers: {transformers.__version__}')" && \
    python3 -c "import fastapi, flask, uvicorn, starlette; print('Web deps: OK')" && \
    python3 -c "from weasyprint import HTML; from xhtml2pdf import pisa; from fpdf import FPDF; import markdown, tabulate; print('Doc deps: OK')" && \
    python3 -c "import httpx, openai, boto3, pydantic, cryptography; from playwright.sync_api import sync_playwright; print('Utility deps: OK')" && \
    python3 -c "import pypdf, pdfplumber, reportlab, openpyxl, pptx, pypdfium2; print('Claude Skills Python deps: OK')" && \
    python3 -c "import dashscope; import google.genai; print('AI skill SDK deps: OK')" && \
    python3 -c "import yfinance; print('yfinance: OK')" && \
    npm config get prefix && pnpm --version && yarn --version && corepack --version && \
    which pdftoppm tesseract soffice pandoc qpdf && \
    echo "Claude Skills system deps: OK" && \
    summarize --help >/dev/null && echo "summarize CLI: OK" && \
    node -e "require('pdf-lib'); console.log('pdf-lib: OK')" && \
    node -e "console.log('remotion:', require('@remotion/cli/package.json').version)" && \
    echo "Remotion CLI: OK" && \
    node -e "require('typescript'); console.log('typescript: OK')" && \
    which vite && echo "vite: OK" && \
    which create-vite && echo "create-vite: OK" && \
    # 验证 ultimate skills（通过符号链接）
    ls /opt/claude_skills/pdf-ultimate/SKILL.md && echo "pdf-ultimate skill: OK" && \
    ls /opt/claude_skills/docx-ultimate/SKILL.md && echo "docx-ultimate skill: OK" && \
    ls /opt/claude_skills/pptx-ultimate/SKILL.md && echo "pptx-ultimate skill: OK" && \
    ls /opt/claude_skills/xlsx-ultimate/SKILL.md && echo "xlsx-ultimate skill: OK" && \
    ls /opt/claude_skills/android-native-dev/SKILL.md && echo "android-native-dev skill: OK" && \
    # 验证已有顶层 skills
    ls /opt/claude_skills/frontend-design/SKILL.md && echo "frontend-design skill: OK" && \
    ls /opt/claude_skills/frontend-design-ultimate-1.0.0/SKILL.md && echo "frontend-design-ultimate skill: OK" && \
    ls /opt/claude_skills/ppt-generator-image-model/SKILL.md && echo "ppt-generator-image-model skill: OK" && \
    ls /opt/claude_skills/find-skills-0.1.0/SKILL.md && echo "find-skills skill: OK" && \
    ls /opt/claude_skills/remotion/SKILL.md && echo "Remotion skill files: OK" && \
    # 验证旧 skills 已移除
    test ! -d /opt/claude_skills/pdf && \
    test ! -d /opt/claude_skills/docx && \
    test ! -d /opt/claude_skills/pptx && \
    test ! -d /opt/claude_skills/xlsx && \
    echo "Old skills removed: OK" && \
    # 验证非 skill 目录已清理
    test ! -d /opt/claude_skills/workspace && \
    test ! -d /opt/claude_skills/minimax_skills_backup && \
    echo "Cleanup verified: OK" && \
    echo "Claude Skills files: OK (CLAUDE_SKILLS_DIR=/opt/claude_skills)"
