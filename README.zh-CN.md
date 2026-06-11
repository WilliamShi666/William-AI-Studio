# William's AI Studio

[English README](README.md)

William's AI Studio 是一个开源 monorepo，包含三个主要项目：AI Agent 平台、本地 Claude Code 工作流 UI，以及面向 A-Level/AP 多学科教学的图文助教系统。

这个仓库采用“源码优先”的开源方式。真实密钥、运行日志、生成轨迹、构建产物、数据库卷和未经审核的数据文件不会放进普通 git 历史。

## 项目组成

| 公开名称 | 路径 | 作用 |
|----------|------|------|
| Roys Alpha | `WilliamManus/` | 主 Agent Web/Backend 平台，支持模型对话、Agent 运行、文件、沙箱工作流和编排。 |
| Claude Code UI | `cc-flow-src/` | 本地 Claude Code 可视化工作流 UI，包含 Web/desktop 开发体验。 |
| Roys Legion | `multimodalrag/` | A-Level/AP 多学科图文助教，包含 OCR、PDF 转 Markdown、切块、Milvus 检索、图片服务和多模态对话。 |
| Roys Legion Demo Dataset | `multimodalrag/datasets/public_tutoring_demo/` | 公开教学数据集的工具、manifest、占位目录和 Milvus 导入脚本。完整 PDF/图片/chunk 数据通过 GitHub Release 发布。 |

首轮开源保留现有目录名，公开文档使用上表中的产品名。

## 上游归属

Roys Alpha（`WilliamManus/`）是在 [`kortix-ai/suna`](https://github.com/kortix-ai/suna) 基础上的二次开发。Suna 当前使用 Elastic License 2.0，因此 Roys Alpha 中来源于 Suna 的部分仍受上游许可证和 notice 约束。William's AI Studio 的原创新增部分默认使用本仓库的 Apache-2.0 许可证，除非具体文件或第三方 notice 另有说明。

## 本地开发环境

建议环境：

- Python 3.11+
- Node.js 20+
- Docker
- Redis 8.0.0+
- Nginx（如果需要统一入口）
- Milvus（Roys Legion 检索链路需要）

使用前请复制示例配置：

```bash
cp .env.example .env
```

真实 API Key 不包含在仓库里，需要开发者自己填写。

## 快速启动概览

不要使用 `start_fusion.sh`。它是私有准备阶段的便利脚本，不包含在公开源码候选中。

### Roys Alpha

安装依赖：

```bash
cd WilliamManus/backend
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cd ../frontend
npm install
npm run build
```

运行：

```bash
# 先启动 Redis
redis-server

# 回到仓库根目录
./start_william_prod.sh
./stop_william_prod.sh
```

### Claude Code UI

Claude Code UI 需要本机已安装并登录 Claude Code CLI。

```bash
cd cc-flow-src
npm install -g @anthropic-ai/claude-code
claude
pnpm install
pnpm dev
```

### Roys Legion

安装依赖：

```bash
cd multimodalrag/backend
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cd ../frontend
npm install
npm run build
```

运行：

```bash
cd multimodalrag/backend
./start_all_services.sh
./stop_all_services.sh
```

如果要处理新的 PDF 并扩充知识库，需要部署至少一种 OCR/PDF 提取链路：DeepSeek OCR、PaddleOCR/PaddleOCR-VL 或 MinerU。

## Nginx 统一入口

公开示例配置：

```text
deploy/nginx/fusion-agent.conf
```

启动 Roys Alpha、Roys Legion、Redis 和相关后端服务后，可以把 Nginx 配置安装到本机：

```bash
sudo cp deploy/nginx/fusion-agent.conf /etc/nginx/conf.d/williams-ai-studio.conf
sudo nginx -t
sudo systemctl reload nginx
```

默认路由：

| 浏览器路径 | 本地服务 |
|------------|----------|
| `/` | Roys Alpha frontend，`127.0.0.1:3000` |
| `/api/` | Roys Alpha backend，`127.0.0.1:8002` |
| `/tutor/` | Roys Legion frontend，`127.0.0.1:5173` |
| `/mr-api/milvus/` | Roys Legion Milvus API，`127.0.0.1:8000` |
| `/mr-api/chunk/` | Roys Legion chunking API，`127.0.0.1:8001` |
| `/mr-api/extraction/` | Roys Legion extraction API，`127.0.0.1:8006` |
| `/mr-api/chat/` | Roys Legion chat API，`127.0.0.1:8501` |
| `/mr-api/debate/` 和 `/debate-api/` | Roys Legion debate API，`127.0.0.1:8602` |

Claude Code UI 是单独的本地开发控制台，当前不通过这个 Nginx 聚合配置代理。

## Roys Legion Demo Dataset

Roys Legion 的核心不是普通 RAG demo，而是多学科图文助教：学生可以选择模型和学科，系统从 PDF 教材/资料中检索文字 chunk 和图片，再生成图文并茂的讲解。

公开数据集已经通过 GitHub Release 发布：

```text
https://github.com/WilliamShi666/William-AI-Studio/releases/tag/roys-legion-demo-dataset-v0.1.0
```

Release 内容：

| 内容 | 数量 / 大小 |
|------|-------------|
| 原始 PDF | `504` |
| Chunk JSONL 文件 | `504` |
| Chunks | `6181` |
| 提取图片 | `3943` |
| 解压后大小 | 约 `3.3G` |
| 被排除文档 | `10`，记录在 `EXCLUDED_DOCUMENTS.json` |

需要下载：

- `SHA256SUMS`
- `roys-legion-demo-dataset-v0.1.0.tar.zst.part-aa` 到 `roys-legion-demo-dataset-v0.1.0.tar.zst.part-az`

合并、校验、解压：

```bash
cat roys-legion-demo-dataset-v0.1.0.tar.zst.part-* > roys-legion-demo-dataset-v0.1.0.tar.zst
sha256sum -c SHA256SUMS
tar --zstd -xf roys-legion-demo-dataset-v0.1.0.tar.zst
```

校验数据集：

```bash
cd public_tutoring_demo
python scripts/validate_dataset.py --manifest manifest.json
```

导入 Milvus：

```bash
python scripts/import_milvus.py \
  --manifest manifest.json \
  --milvus-api-url http://localhost:8000 \
  --collection-name roys_legion_demo_v0_1_0
```

不连接 Milvus 的 dry-run：

```bash
python scripts/import_milvus.py \
  --manifest manifest.json \
  --collection-name roys_legion_demo_v0_1_0 \
  --dry-run
```

Milvus 保存向量和 metadata，不保存图片二进制。图片在解压后的：

```text
extraction_results/{file_id}/images/
```

检索 chunk 引用图片时，Roys Legion 后端通过下面的接口提供图片：

```text
/document/{file_id}/images/{image_name}
```

因此 Release 同时包含 chunks 和 extracted images。

## OCR 和模型服务

Roys Legion 支持或集成过以下 OCR/PDF 提取路径：

- MinerU
- DeepSeek OCR
- PaddleOCR / PaddleOCR-VL

这些模型服务不作为大模型 blob 直接提交进本仓库。公开使用方式应通过文档下载模型、校验模型，并在本地部署服务。

部分 PDF 快速处理路径使用 PyMuPDF/MuPDF 相关依赖，许可证为 AGPL 或商业授权。默认依赖不会安装这些包。如需启用相关路径，请根据自己的使用场景确认 AGPL 义务或商业授权。

## 项目结构

```text
.
├── WilliamManus/      # Roys Alpha Web 前端、FastAPI 后端、Agent runtime 和工作流编排
├── cc-flow-src/       # Claude Code UI 本地可视化编码工作流应用
├── multimodalrag/     # Roys Legion 图文助教、OCR/RAG 后端、前端、Milvus 工具和数据集工具
├── deploy/            # 清理后的部署示例
└── scripts/           # 公开候选检查和开发者工具脚本
```

## 许可证

本仓库使用 Apache License 2.0。详见 `LICENSE`。

依赖项和上游派生部分仍需遵守对应第三方许可证和 notice。
