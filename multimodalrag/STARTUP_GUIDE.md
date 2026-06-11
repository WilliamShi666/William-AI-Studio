# MultimodalRAG 服务启动指南

SSH 断开后服务仍可运行，可通过本机地址访问（http://localhost/tutor/）。

---

## 方法一：使用 tmux（推荐）

使用 tmux 可以确保服务在 SSH 断开后继续稳定运行。

### tmux 基础命令

```bash
# 安装 tmux（如果未安装）
sudo apt install tmux

# 创建新会话
tmux new -s <会话名>

# 列出所有会话
tmux ls

# 附加到会话
tmux attach -t <会话名>

# 在会话中分离（保持运行）
Ctrl+b 然后按 d

# 关闭会话
tmux kill-session -t <会话名>
```

### 启动后端服务（tmux）

MultimodalRAG 后端默认启动 4 个核心服务：PDF 提取、文本切分、Milvus API 和对话检索。
多智能体辩论服务依赖 AgentScope，属于可选组件，建议使用独立 Python 环境安装 `backend/requirements-debate.txt` 后再启动。

```bash
# 创建后端会话
tmux new -s mr-backend

# 在 tmux 会话中执行：
cd /path/to/williams-ai-studio/multimodalrag/backend
bash start_all_services.sh

# 按 Ctrl+b 然后按 d 分离会话
```

或者分别启动各个服务（便于单独调试）：

```bash
# 激活 Python 环境
PYTHON_CMD="/path/to/python-env/bin/python"
BACKEND_DIR="/path/to/williams-ai-studio/multimodalrag/backend"

# 1. PDF提取服务 (端口 8006)
tmux new -d -s mr-pdf -c "$BACKEND_DIR/Information-Extraction/unified" "$PYTHON_CMD unified_pdf_extraction_service.py"

# 2. 文本切分服务 (端口 8001)
tmux new -d -s mr-chunk -c "$BACKEND_DIR/Text_segmentation" "$PYTHON_CMD markdown_chunker_api.py"

# 3. Milvus向量数据库服务 (端口 8000)
tmux new -d -s mr-milvus -c "$BACKEND_DIR/Database/milvus_server" "$PYTHON_CMD milvus_api.py"

# 4. 对话检索服务 (端口 8501)
tmux new -d -s mr-chat -c "$BACKEND_DIR/chat" "$PYTHON_CMD kb_chat.py"

# 5. 多智能体辩论服务 (端口 8602，可选)
# 先在独立环境中安装:
# pip install -r "$BACKEND_DIR/requirements-debate.txt"
tmux new -d -s mr-debate -c "$BACKEND_DIR/multiagent_debaters" "$PYTHON_CMD -m uvicorn multiagent_debaters.api:app --host 127.0.0.1 --port 8602"
```

### 启动前端服务（tmux）

```bash
# 创建前端会话
tmux new -s mr-frontend

# 在 tmux 会话中执行：
cd /path/to/williams-ai-studio/multimodalrag/frontend
npm run dev

# 按 Ctrl+b 然后按 d 分离会话
```

### 一键启动脚本（tmux）

创建文件 `start_all_tmux.sh`：

```bash
#!/bin/bash
# MultimodalRAG 一键启动脚本 (tmux)

BACKEND_DIR="/path/to/williams-ai-studio/multimodalrag/backend"
FRONTEND_DIR="/path/to/williams-ai-studio/multimodalrag/frontend"

# 启动后端（使用现有脚本）
tmux new -d -s mr-backend -c "$BACKEND_DIR" "bash start_all_services.sh; exec bash"

# 启动前端
tmux new -d -s mr-frontend -c "$FRONTEND_DIR" "npm run dev"

echo "所有服务已在 tmux 中启动"
echo ""
echo "查看会话: tmux ls"
echo "附加到后端: tmux attach -t mr-backend"
echo "附加到前端: tmux attach -t mr-frontend"
```

---

## 方法二：使用现有脚本（nohup）

> 注意：nohup 在某些情况下（如 VS Code SSH 断开）可能导致服务终止。推荐使用 tmux。

### 启动后端服务

```bash
cd /path/to/williams-ai-studio/multimodalrag/backend
bash start_all_services.sh
```

### 启动前端服务

```bash
cd /path/to/williams-ai-studio/multimodalrag/frontend
bash start_frontend.sh

# 或直接使用 nohup
nohup npm run dev > frontend.log 2>&1 &
```

---

## 服务管理

### 查看日志

**tmux 方式：**
```bash
# 附加到会话查看实时日志
tmux attach -t mr-backend
tmux attach -t mr-frontend
# 按 Ctrl+b 然后按 d 分离
```

**nohup 方式：**
```bash
# 后端日志
tail -f /path/to/williams-ai-studio/multimodalrag/backend/logs/*.log

# 单个服务日志
tail -f /path/to/williams-ai-studio/multimodalrag/backend/logs/chat.log
tail -f /path/to/williams-ai-studio/multimodalrag/backend/logs/milvus_api.log
tail -f /path/to/williams-ai-studio/multimodalrag/backend/logs/pdf_extraction.log
tail -f /path/to/williams-ai-studio/multimodalrag/backend/logs/chunker.log
tail -f /path/to/williams-ai-studio/multimodalrag/backend/logs/debate.log

# 前端日志
tail -f /path/to/williams-ai-studio/multimodalrag/frontend/frontend.log
```

### 停止服务

**tmux 方式：**
```bash
# 停止所有 mr- 开头的会话
tmux kill-session -t mr-backend
tmux kill-session -t mr-frontend

# 如果分别启动了各服务
tmux kill-session -t mr-pdf
tmux kill-session -t mr-chunk
tmux kill-session -t mr-milvus
tmux kill-session -t mr-chat
tmux kill-session -t mr-debate
```

**使用脚本：**
```bash
cd /path/to/williams-ai-studio/multimodalrag/backend
bash stop_all_services.sh
```

**按端口停止：**
```bash
kill $(lsof -t -i:8006)   # PDF提取
kill $(lsof -t -i:8001)   # 文本切分
kill $(lsof -t -i:8000)   # Milvus
kill $(lsof -t -i:8501)   # 对话检索
kill $(lsof -t -i:8602)   # 辩论服务
kill $(lsof -t -i:5173)   # 前端
```

### 检查服务状态

```bash
# 使用现有脚本
cd /path/to/williams-ai-studio/multimodalrag/backend
bash status_services.sh

# 检查端口占用
lsof -i :8006   # PDF提取
lsof -i :8001   # 文本切分
lsof -i :8000   # Milvus
lsof -i :8501   # 对话检索
lsof -i :8602   # 辩论服务
lsof -i :5173   # 前端

# 检查 tmux 会话
tmux ls

# 检查进程
ps aux | grep -E "(milvus|chunker|chat|uvicorn|vite)" | grep -v grep
```

---

## 端口汇总

| 服务 | 端口 | 说明 |
|------|------|------|
| 前端 (Vite) | 5173 | React 前端界面 |
| PDF提取服务 | 8006 | 处理 PDF 文档提取 |
| 文本切分服务 | 8001 | Markdown 文本分块 |
| Milvus API | 8000 | 向量数据库接口 |
| 对话检索服务 | 8501 | 知识库对话接口 |
| 多智能体辩论 | 8602 | 辩论系统接口 |
| DeepSeek-OCR（可选）| 8705 | 高级 OCR 服务 |

---

## 常见问题排查

### 服务无法访问 (502 Bad Gateway)

**症状：** 访问 http://localhost/tutor/ 显示 502 Bad Gateway

**排查步骤：**
```bash
# 1. 检查前端端口 5173 是否有进程监听
lsof -i :5173

# 2. 如果没有，使用 tmux 重新启动前端
tmux new -s mr-frontend
cd /path/to/williams-ai-studio/multimodalrag/frontend
npm run dev
# Ctrl+b, d 分离
```

### 后端服务启动失败

**检查日志：**
```bash
tail -100 /path/to/williams-ai-studio/multimodalrag/backend/logs/<服务名>.log
```

**常见原因：**
1. 端口被占用 - 使用 `lsof -i :<端口>` 检查并 kill 占用进程
2. Python 环境问题 - 确保使用正确的 conda 环境
3. 依赖服务未启动 - Milvus Docker 容器需要先启动

### Milvus 连接错误

```bash
# 检查 Milvus Docker 容器状态
cd /path/to/williams-ai-studio/multimodalrag/backend/Database/milvus_server
docker-compose ps
docker-compose up -d  # 启动（如果未运行）
```

---

## 注意事项

- **推荐使用 tmux** 启动服务，避免 SSH 断开导致服务终止
- 启动前确保端口未被占用
- Milvus 服务依赖 Docker，确保 Milvus 容器正在运行
- Python 环境: `/path/to/python-env`
- 首次启动前端需要先运行 `npm install`
- 日志文件位置：
  - 后端：`backend/logs/*.log`
  - 前端：`frontend/frontend.log`
