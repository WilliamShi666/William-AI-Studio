# PDF 上传和提取服务 - 快速启动指南

## 功能概览

本服务支持：
- PDF 文件上传
- 自动提取内容（快速模式 / 精确模式）
- 自动切分文档（集成切分服务）
- 本地保存文件和提取结果
- 前后端完整对接

---

## 0. 环境配置

### 配置文件：`/backend/.env`

确保以下配置正确：

```bash
# API 配置（用于精确模式提取）
API_KEY=<MODEL_PROVIDER_API_KEY>
MODEL_NAME=qwen3-vl-plus
MODEL_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 上传文件存储路径
UPLOAD_BASE_DIR=./runtime/uploads

# 提取结果存储路径
EXTRACTION_RESULTS_DIR=./runtime/extraction_results

# 文件大小限制（MB）
MAX_FILE_SIZE_MB=50

# 服务端口配置
INFOR_EXTRAC_SERVICE_PORT=8006
INFOR_EXTRAC_SERVICE_HOST=127.0.0.1

# 切分服务配置
CHUNK_SERVICE_HOST=127.0.0.1
CHUNK_SERVICE_PORT=8001
CHUNKING_SERVICE_ENABLED=true
```

**注意**：
- 如果使用精确模式，需要配置有效的 `API_KEY`
- 快速模式不需要 API 配置，但依赖 PyMuPDF/PyMuPDF4LLM；这些依赖采用 AGPL 或商业许可，不在默认 `requirements.txt` 中安装
- 如需启用快速模式，请先确认你的使用方式符合 AGPL 义务或已有 Artifex 商业许可，然后执行：`pip install -r multimodalrag/backend/requirements-pdf-agpl.txt`
- 如果要使用自动切分功能，需要启动切分服务（端口 8001）
- `CHUNK_SERVICE_HOST=127.0.0.1` 表示切分服务默认只监听本机。如需从其他机器访问，请显式改成合适的局域网地址或 `0.0.0.0`

---

## 1. 启动后端服务

### 修改启动配置

打开 `/backend/Information-Extraction/unified/unified_pdf_extraction_service.py`

找到文件末尾，确保：

```python
if __name__ == "__main__":
    is_debug = False  # 必须为 False 才能启动 Web 服务
    # ...
```

### 启动后端

```bash
cd multimodalrag/backend/Information-Extraction/unified

python unified_pdf_extraction_service.py
```

后端将在 **http://localhost:8006** 启动

你应该看到类似的输出：
```
配置加载完成:
  - 上传目录: ./runtime/uploads
  - 提取结果目录: ./runtime/extraction_results
  - 文件大小限制: 50MB
  - 服务地址: 127.0.0.1:8006
启动PDF提取服务...
INFO:     Started server process
INFO:     Uvicorn running on http://127.0.0.1:8006
```

---

## 2. 启动前端服务

前端已经在运行中（端口 5173），如果没有运行：

```bash
cd multimodalrag/frontend

npm run dev
```

前端将在 **http://localhost:5173** 启动

---

## 3. 测试上传功能

### 在浏览器中

1. 打开 **http://localhost:5173**
2. 进入 **知识库管理** 页面
3. 点击 **上传文档** 按钮
4. 选择一个知识库
5. **选择提取模式**（重要！）：
   - **快速模式(PyMuPDF4LLM)**：速度快，适合简单文档，无需 API 配置；需要额外安装 AGPL/商业许可依赖
   - **精确模式(VLM)**：准确度高，适合复杂文档，需要 `.env` 中配置 API
6. 拖拽或选择 PDF 文件
7. 点击 **上传** 按钮


## 4. 检查上传结果

### 查看上传的文件

```bash
ls -lh ./runtime/uploads/
```

### 查看提取结果

```bash
ls -lh ./runtime/extraction_results/
```

每个文件夹包含：
- `{filename}.md` - Markdown 提取结果
- `metadata.json` - 元数据
- `images/` - 截图和提取的图片

---

## 5. 后端 API 端点

### 上传文件

```
POST http://localhost:8006/api/v1/files/upload
```

**请求参数（multipart/form-data）：**
- `file` (必填): PDF 文件
- `knowledge_base_id` (可选): 知识库 ID
- `auto_extract` (可选): 是否自动提取，默认 `false`
- `extraction_mode` (可选): 提取模式 `fast` 或 `accurate`/`vlm`，默认 `fast`
- `auto_chunk` (可选): 是否自动切分，默认 `false`（需要 `auto_extract=true`）
- `chunking_method` (可选): 切分方法 `header_recursive` 或 `markdown_only`，默认 `header_recursive`
- `chunk_size` (可选): 目标chunk大小，默认 `1500`
- `chunk_overlap` (可选): chunk重叠长度，默认 `200`
- `max_page_span` (可选): 最大跨页数，默认 `3`

**extraction_mode 说明**：
- `fast`: 使用 PyMuPDF4LLM，无需 API 配置，速度快；需要额外安装 `requirements-pdf-agpl.txt`
- `accurate` 或 `vlm`: 使用 VLM 大模型，需要在 `.env` 中配置 `API_KEY`, `MODEL_NAME`, `MODEL_URL`

**chunking_method 说明**：
- `header_recursive`: 基于标题的递归切分（推荐）
- `markdown_only`: 纯 Markdown 切分

**响应示例（带切分）：**
```json
{
  "success": true,
  "message": "文件上传成功并已完成提取和切分",
  "data": {
    "file_id": "file_20251016_abc12345",
    "filename": "test.pdf",
    "file_size": 2048576,
    "file_path": "/path/to/file.pdf",
    "upload_time": "2025-10-16T10:30:00",
    "extraction": {
      "status": "completed",
      "mode": "fast",
      "total_pages": 10,
      "total_images": 25,
      "markdown_path": "/path/to/result.md",
      "images_dir": "/path/to/images"
    },
    "chunking": {
      "status": "completed",
      "method": "header_recursive",
      "total_chunks": 15,
      "cross_page_chunks": 3,
      "avg_chunk_length": 1456.8,
      "chunks_path": "/path/to/chunks.json"
    }
  }
}
```

---

## 6. 使用 curl 测试

### 仅上传文件（不提取）

```bash
curl -X POST "http://localhost:8006/api/v1/files/upload" \
  -F "file=@/path/to/test.pdf" \
  -F "knowledge_base_id=kb_001"
```

### 上传并快速提取

```bash
curl -X POST "http://localhost:8006/api/v1/files/upload" \
  -F "file=@/path/to/test.pdf" \
  -F "auto_extract=true" \
  -F "extraction_mode=fast"
```

### 上传并精确提取（使用 .env 中的 API 配置）

```bash
curl -X POST "http://localhost:8006/api/v1/files/upload" \
  -F "file=@/path/to/complex_doc.pdf" \
  -F "auto_extract=true" \
  -F "extraction_mode=accurate"
```

### 上传 + 提取 + 切分（一站式流程）

```bash
curl -X POST "http://localhost:8006/api/v1/files/upload" \
  -F "file=@/path/to/test.pdf" \
  -F "auto_extract=true" \
  -F "extraction_mode=fast" \
  -F "auto_chunk=true" \
  -F "chunking_method=header_recursive" \
  -F "chunk_size=1500" \
  -F "chunk_overlap=200"
```

### 使用自定义 API 配置的精确提取

```bash
curl -X POST "http://localhost:8006/extract/accurate" \
  -F "file=@/path/to/test.pdf" \
  -F "api_key=your-api-key" \
  -F "model_name=gpt-4o" \
  -F "model_url=https://api.openai.com/v1" \
  -F "save_file=true" \
  -F "knowledge_base_id=kb_001"
```

---

## 7. 前端配置说明

### API 地址配置

在 `UploadDialog.tsx` 中：

```typescript
const API_BASE_URL = 'http://localhost:8006';
```

如果后端端口改变，修改这个地址即可。

### 提取模式

- **快速模式 (fast)**: 使用 PyMuPDF4LLM，速度快，适合简单文档，✅ 支持自动提取
- **精确模式 (vlm/accurate)**: 使用 VLM 大模型，准确度高，适合复杂文档，✅ 支持自动提取（需配置 API）

**两种模式对比**：

| 特性 | 快速模式 | 精确模式 |
|------|---------|---------|
| 速度 | ⚡ 快 | 🐢 慢 |
| 需要 API | ❌ 否 | ✅ 是 |
| 表格识别 | 基础 | 精确 |
| 公式识别 | 不支持 | 支持 |
| 图片处理 | 提取 + 截图 | 描述 |
| 适用场景 | 简单文档、快速预览 | 学术论文、技术文档 |

---

## 8. 常见问题

### 后端无法启动

**检查端口占用：**
```bash
netstat -tlnp | grep 8006
```

**更改端口：**
修改 `/backend/.env`：
```bash
INFOR_EXTRAC_SERVICE_PORT=8007  # 改为其他端口
```

### 前端无法连接后端

**检查 CORS 配置：**
后端已配置允许所有来源，如果还有问题，检查浏览器控制台的错误信息。

**检查后端是否运行：**
```bash
curl http://localhost:8006/
```

应该返回：
```json
{"status":"running","service":"PDF Extraction API","version":"1.0.0"}
```

### 文件上传失败

**检查文件大小：**
当前限制 50MB，如需增大：

修改 `/backend/.env`：
```bash
MAX_FILE_SIZE_MB=100  # 改为 100MB
```

**检查文件格式：**
仅支持 `.pdf` 格式

**检查磁盘空间：**
```bash
df -h /home/MuyuWorkSpace
```

---

## 9. 目录结构

```
Multimodal_RAG/
├── backend/
│   ├── uploads/                    # 上传的 PDF 文件
│   │   └── 2025/10/16/
│   │       └── file_20251016_abc12345_test.pdf
│   ├── extraction_results/         # 提取结果
│   │   └── file_20251016_abc12345/
│   │       ├── test.md             # Markdown 提取结果
│   │       ├── metadata.json       # 元数据
│   │       ├── chunks.json         # 切分结果（如果启用）
│   │       └── images/             # 截图和图片
│   │           ├── page_1_full.png
│   │           └── ...
│   ├── Information-Extraction/     # 提取服务
│   │   └── unified/
│   │       └── unified_pdf_extraction_service.py
│   └── Text_segmentation/          # 切分服务
│       └── markdown_chunker_api.py
└── frontend/
    ├── components/
    │   └── UploadDialog.tsx       # 上传组件
    └── App.tsx
```

---

---

## 10. API 端点总结

| 端点 | 方法 | 功能 | 保存文件 | 提取模式 | 支持切分 |
|------|------|------|---------|---------|---------|
| `/api/v1/files/upload` | POST | 上传+可选提取+可选切分 | ✅ | fast / accurate | ✅ |
| `/extract/fast` | POST | 仅快速提取 | ❌ | fast | ❌ |
| `/extract/accurate` | POST | 精确提取+可选保存 | 可选 | accurate | ❌ |
| `/` | GET | 健康检查 | - | - | - |
| `/health` | GET | 健康检查 | - | - | - |

---


## 提示

1. **快速开始**: 如果只是测试，使用快速模式即可，无需配置 API
2. **精确提取**: 处理复杂文档时，建议使用精确模式，效果更好
3. **监控日志**: 后端会打印详细的处理日志，方便调试
4. **检查存储**: 提取结果保存在 `extraction_results/` 目录中，可以直接查看
