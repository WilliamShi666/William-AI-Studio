# RAG对话服务API接口文档

## 概述

RAG对话服务是一个基于FastAPI构建的API服务，提供向量召回、重排序、流式/非流式问答等功能。

### 主要特性
- 向量召回文档
- 重排序优化
- 流式和非流式响应
- 历史对话支持
- 来源文档返回

### 服务文件
- `chat_api.py`: 主服务文件
- `test_kb_chat_api.py`: 测试文件

启动参考命令
```bash
nohup uvicorn kb_chat:app --host 127.0.0.1 --port 8501 > chat_api.log &
```


## API端点

### 健康检查

#### GET /

获取服务基本信息和状态。

**响应示例:**
```json
{
  "status": "running",
  "service": "RAG Chat API",
  "version": "1.0.0",
  "features": ["vector_retrieval", "reranking", "streaming", "non_streaming"]
}
```

#### GET /health

服务健康检查。

**响应示例:**
```json
{
  "status": "healthy",
  "service": "rag-chat",
  "timestamp": "2023-10-27T10:00:00.000000"
}
```

### RAG对话接口

#### POST /chat

RAG对话接口，支持流式和非流式两种模式。

##### 请求参数

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| query | string | 是 | 用户查询问题 |
| collection_name | string | 是 | Milvus集合名称 |
| milvus_api_url | string | 是 | Milvus API地址 |
| llm_config | object | 是 | 大语言模型配置 |
| top_k | integer | 否 | 召回文档数量，默认为30 |
| score_threshold | number | 否 | 召回文档的相似度阈值，默认为0.3 |
| stream | boolean | 否 | 是否使用流式响应，默认为False |
| return_source | boolean | 否 | 是否返回来源文档，默认为True |
| use_reranker | boolean | 否 | 是否使用重排序，默认为True |
| reranker_config | object | 否 | 重排序配置，当use_reranker为true时必需 |
| history | array | 否 | 历史对话记录 |
| prompt_template | string | 否 | 自定义Prompt模板 |

##### LLM配置 (llm_config)

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| api_url | string | 是 | LLM API地址 |
| api_key | string | 是 | LLM API密钥 |
| model_name | string | 是 | 模型名称 |
| temperature | number | 否 | 温度参数，默认为0.7 |
| max_tokens | integer | 否 | 最大token数，默认为1024 |

##### 重排序配置 (reranker_config)

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| api_url | string | 是 | 重排序API地址 |
| api_key | string | 是 | 重排序API密钥 |
| model_name | string | 是 | 重排序模型名称 |
| top_n | integer | 是 | 重排序后保留的文档数量 |

##### 历史对话记录 (history)

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| role | string | 是 | 角色，可选值：user, assistant |
| content | string | 是 | 对话内容 |

##### 响应格式

###### 非流式响应

```json
{
  "success": true,
  "message": "对话完成",
  "answer": "这是模型的回答内容...",
  "sources": [
    {
      "chunk_text": "文档片段内容...",
      "filename": "source_file.pdf",
      "score": 0.85,
      "metadata": {}
    }
  ],
  "metadata": {
    "retrieve_time": 0.5,
    "rerank_time": 0.2,
    "llm_time": 1.2,
    "total_time": 1.9,
    "documents_count": 5
  }
}
```

###### 流式响应

流式响应使用NDJSON格式（换行分隔的JSON），每行是一个事件：

- 内容片段: `{"type": "content", "data": "..."}`
- 来源文档: `{"type": "sources", "data": [...]}`
- 元数据: `{"type": "metadata", "data": {...}}`
- 错误信息: `{"type": "error", "data": {...}}`

## 使用示例

### Python客户端示例

```python
import requests
import json

# 非流式请求
response = requests.post(
    "http://localhost:8501/chat",
    json={
        "query": "什么是代码自解释?",
        "collection_name": "knowledge_base",
        "milvus_api_url": "http://milvus-server:8002",
        "llm_config": {
            "api_url": "https://api.openai.com/v1",
            "api_key": "your-api-key",
            "model_name": "gpt-3.5-turbo"
        },
        "top_k": 30,
        "score_threshold": 0.3,
        "stream": False,
        "return_source": True
    }
)

if response.status_code == 200:
    result = response.json()
    print("回答:", result["answer"])
    print("来源:", result["sources"])

# 流式请求
response = requests.post(
    "http://localhost:8501/chat",
    json={
        "query": "代码自解释的应用场景有哪些?",
        "collection_name": "knowledge_base",
        "milvus_api_url": "http://milvus-server:8002",
        "llm_config": {
            "api_url": "https://api.openai.com/v1",
            "api_key": "your-api-key",
            "model_name": "gpt-3.5-turbo"
        },
        "stream": True,
        "return_source": True
    },
    stream=True
)

for line in response.iter_lines():
    if line:
        event = json.loads(line)
        if event["type"] == "content":
            print(event["data"], end="", flush=True)
```

### 带历史对话的请求示例

```python
response = requests.post(
    "http://localhost:8501/chat",
    json={
        "query": "它有哪些具体实践技巧?",
        "collection_name": "knowledge_base",
        "milvus_api_url": "http://milvus-server:8002",
        "llm_config": {
            "api_url": "https://api.openai.com/v1",
            "api_key": "your-api-key",
            "model_name": "gpt-3.5-turbo"
        },
        "history": [
            {
                "role": "user",
                "content": "代码自解释是什么?"
            },
            {
                "role": "assistant",
                "content": "代码自解释是一种编程实践，强调编写清晰易懂的代码，使代码本身就能表达其意图和功能，减少对注释的依赖。"
            }
        ],
        "stream": False,
        "return_source": True
    }
)
```

### 使用重排序的请求示例

```python
response = requests.post(
    "http://localhost:8501/chat",
    json={
        "query": "代码自解释的实现方法有哪些?",
        "collection_name": "knowledge_base",
        "milvus_api_url": "http://milvus-server:8002",
        "llm_config": {
            "api_url": "https://api.openai.com/v1",
            "api_key": "your-api-key",
            "model_name": "gpt-3.5-turbo"
        },
        "top_k": 30,
        "score_threshold": 0.2,
        "use_reranker": True,
        "reranker_config": {
            "api_url": "https://api.jina.ai/v1",
            "api_key": "your-jina-api-key",
            "model_name": "jina-reranker-v2-base-multilingual",
            "top_n": 5
        },
        "stream": False,
        "return_source": True
    }
)
```

## 启动服务

```bash
cd /home/data/nongwa/workspace/chat
python kb_chat.py
```

服务默认监听在 `127.0.0.1:8501`，可以通过环境变量 `SERVER_HOST` 和 `SERVER_PORT` 进行配置。

访问API文档: http://localhost:8501/docs
        
