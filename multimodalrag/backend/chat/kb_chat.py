"""
RAG对话模块 - FastAPI接口版本
支持向量召回、可选重排序、流式/非流式问答
"""
import asyncio
import json
import time
import uuid
import os
import logging
from typing import List, Dict, Any, Optional, AsyncIterable
from datetime import datetime

import uvicorn
import httpx
import aiofiles
import jwt
from fastapi import FastAPI, HTTPException, Body, Request, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from qa_db import (
    create_session,
    delete_session,
    get_messages,
    get_session,
    insert_message,
    list_sessions,
    purge_old_sessions,
    touch_session,
)
from qa_service import QAService

# 服务配置
SERVICE_PORT = int(os.getenv("CHAT_SERVICE_PORT", "8501"))
SERVICE_HOST = os.getenv("CHAT_SERVICE_HOST", "127.0.0.1")

UNSAFE_JWT_SECRETS = {
    "-".join(parts)
    for parts in (
        ("your", "secret", "key", "change", "in", "production"),
        ("your", "secret", "key", "change", "this", "in", "production"),
        ("your", "super", "secret", "jwt", "key", "change", "this", "in", "production"),
    )
}
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
PUBLIC_PATHS = {"/", "/health", "/config/default", "/docs", "/openapi.json", "/redoc"}


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/docs")


def _require_jwt_secret() -> str:
    if not JWT_SECRET_KEY or JWT_SECRET_KEY in UNSAFE_JWT_SECRETS or len(JWT_SECRET_KEY) < 32:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY is not configured safely")
    return JWT_SECRET_KEY


async def verify_jwt(request: Request) -> str:
    if request.method == "OPTIONS" or _is_public_path(request.url.path):
        return "anonymous"

    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(
            status_code=401,
            detail="No valid authentication credentials found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, _require_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id

# ============ 数据模型 ============

class Message(BaseModel):
    """对话消息"""
    role: str = Field(..., description="角色: user/assistant/system")
    content: str = Field(..., description="消息内容")

class LLMConfig(BaseModel):
    """大模型配置"""
    api_url: str = Field(..., description="LLM API地址")
    api_key: str = Field(..., description="LLM API密钥")
    model_name: str = Field(..., description="模型名称")
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="采样温度")
    max_tokens: int = Field(5000, ge=1, description="最大生成token数")

class RerankerConfig(BaseModel):
    """重排序配置"""
    api_url: str = Field(..., description="Reranker API地址")
    api_key: str = Field(..., description="Reranker API密钥")
    model_name: str = Field(..., description="Reranker模型名称")
    top_n: int = Field(5, ge=1, description="重排序后保留的文档数量")

class SourceDocument(BaseModel):
    """来源文档"""
    chunk_text: str
    filename: str
    score: float  # 主分数（如果有重排序则为重排序分数，否则为召回分数）
    retrieval_score: Optional[float] = None  # 原始召回分数
    rerank_score: Optional[float] = None  # 重排序分数
    metadata: Dict[str, Any] = {}
    
class ChatRequest(BaseModel):
    """对话请求"""
    query: str = Field(..., description="用户问题")
    collection_name: str = Field(..., description="Milvus集合名称")
    llm_config: LLMConfig = Field(..., description="大模型配置")
    
    # 召回配置
    top_k: int = Field(30, ge=1, le=50, description="召回文档数量")
    score_threshold: float = Field(0.5, ge=0.0, le=1.0, description="相似度阈值")
    
    # 重排序配置
    use_reranker: bool = Field(True, description="是否使用重排序")
    reranker_config: Optional[RerankerConfig] = Field(None, description="重排序配置")
    
    # 对话配置
    history: List[Message] = Field(default=[], description="历史对话")
    stream: bool = Field(True, description="是否流式输出")
    prompt_template: Optional[str] = Field(None, description="自定义prompt模板")
    return_source: bool = Field(True, description="是否返回来源文档")
    
    # Milvus服务地址
    milvus_api_url: str = Field("http://localhost:8000", description="Milvus API地址")

    # 苏格拉底问答法模式
    socratic_mode: bool = Field(False, description="是否启用苏格拉底问答法")

    # 思考模式
    enable_thinking: bool = Field(False, description="是否启用思考模式")

class ChatResponse(BaseModel):
    """对话响应（非流式）"""
    success: bool
    message: str
    answer: str
    sources: Optional[List[SourceDocument]] = None
    metadata: Dict[str, Any] = {}


class QAFile(BaseModel):
    filename: str
    data: str


class QAMessage(BaseModel):
    role: str
    content: str
    images: Optional[List[str]] = None
    files: Optional[List[QAFile]] = None
    reasoning_content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class QARequest(BaseModel):
    query: str
    model_key: str
    images: Optional[List[str]] = None
    files: Optional[List[QAFile]] = None
    session_id: Optional[str] = None
    stream: bool = True
    enable_thinking: bool = False
    question_type: Optional[str] = None  # 经济答题区题型标识


# ============ 经济答题区题型配置 ============

ECON_QUESTION_TYPES = {
    "as_data_response": ["CIE_AS_Dataresponse_Prompt.md"],
    "as_8_marker": ["CIE_AS_Econ_8_Marker_Prompt.md"],
    "as_12_marker": ["CIE_AS_Econ_12_Marker_Prompt.md"],
    "full_as_paper_2": [
        "CIE_AS_Dataresponse_Prompt.md",
        "CIE_AS_Econ_8_Marker_Prompt.md",
        "CIE_AS_Econ_12_Marker_Prompt.md",
    ],
    "a2_data_response": ["CIE_A2_Dataresponse_Prompt.md"],
    "a2_20_marker": ["CIE_A2_Econ_20_Marker_Prompt.md"],
    "full_a2_paper_4": [
        "CIE_A2_Dataresponse_Prompt.md",
        "CIE_A2_Econ_20_Marker_Prompt.md",
    ],
    # 苏格拉底问答法模式
    "socratic_method": ["Socratic_Method_Prompt.md"],
    # 破题结界专用的苏格拉底解题引导
    "qa_socratic_method": ["QA_Socratic_Method_Prompt.md"],
}

# 苏格拉底模式触发关键词
SOCRATIC_TRIGGERS = [
    "socrates!",
    "socrates！",
    "启动苏格拉底问答法",
    "苏格拉底问答法",
    "苏格拉底模式",
    "请逐步引导我理解",
    "逐步引导我",
    "引导式学习",
    "引导我理解",
    "请审视这个知识点",
    "和我深入探讨",
]

async def load_econ_prompts(question_type: str) -> Optional[str]:
    """加载并合并指定题型的prompt文件（异步版本）"""
    if question_type not in ECON_QUESTION_TYPES:
        return None

    prompt_files = ECON_QUESTION_TYPES[question_type]
    prompts_dir = os.path.dirname(os.path.abspath(__file__))

    combined_prompt = []
    for filename in prompt_files:
        filepath = os.path.join(prompts_dir, filename)
        try:
            async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
                content = await f.read()
                content = content.strip()
                if content:
                    combined_prompt.append(content)
        except FileNotFoundError:
            logging.warning(f"Prompt file not found: {filepath}")
            continue
        except Exception as e:
            logging.warning(f"Error reading prompt file {filepath}: {e}")
            continue

    if not combined_prompt:
        return None

    # 如果是Full Paper，添加分隔说明
    if len(combined_prompt) > 1:
        return "\n\n---\n\n".join(combined_prompt)
    return combined_prompt[0]


def detect_socratic_mode(query: str) -> bool:
    """检测用户输入是否要求启动苏格拉底问答法"""
    query_lower = query.lower().strip()
    for trigger in SOCRATIC_TRIGGERS:
        if trigger.lower() in query_lower:
            return True
    return False


class QASessionCreate(BaseModel):
    title: Optional[str] = None
    model_name: Optional[str] = None

# ============ 对话服务 ============

class ChatService:
    """RAG对话服务"""

    def __init__(self):
        # 读取API基础URL配置
        self.api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
        self.default_reranker_config: Optional[RerankerConfig] = None

        self.default_prompt_template = """你是一名国际学科助教，是Roys乐亦思国际学科助教军团中的一员。你的职责是为国际课程学生提供专业的学科答疑服务。

【身份规则】：当用户询问你是谁时，你应回答自己是"国际学科助教，是助教军团中的一员"，而不是说自己是某个AI模型。

请根据以下检索到的相关信息回答用户的问题。

相关信息：
{context}

用户问题：{query}

请基于以上信息给出准确、详细的回答。如果信息不足以回答问题，请如实说明。

【图片嵌入规则 - 必须严格遵守】：
1. 当上下文中出现图片（格式为 ![...](URL) 或 包含 /document/ 路径），你**必须直接在回答中嵌入这些图片**。
2. **禁止**用文字描述图片路径，**禁止**说"图片是内部资源"或"无法访问"，所有提供的图片URL都是可访问的。
3. 直接复制完整的图片markdown语法到你的回答中：![描述](完整URL)
4. 将图片放在相关解释的前后位置。

【错误示例 - 禁止这样回答】：
❌ "图片路径为 images/xxx.png，这是PDF内部资源，无法直接访问"
❌ "相关图片存储在 /document/xxx/images/yyy.png"
❌ "请查看Figure 20.5了解详情"

【正确示例 - 必须这样回答】：
✅ "成本推动型通胀是指...如下图所示：\n\n![Figure 20.5](/document/abc123/images/cost_push.png)\n\n从图中可以看到..."
✅ "GDP平减指数的计算公式为：\n\n![GDP Deflator Formula](/document/abc123/images/formula.png)\n\n这个公式说明..."

记住：直接嵌入图片，不要解释路径！"""
        env_reranker_model = os.getenv("RERANKER_MODEL")
        if env_reranker_model:
            reranker_api_key = (
                os.getenv("RERANKER_API_KEY")
                or os.getenv("API_KEY")
                or os.getenv("EMBEDDING_API_KEY")
                or ""
            )
            reranker_api_url = (
                os.getenv("RERANKER_API_URL")
                or os.getenv("MODEL_URL")
                or os.getenv("EMBEDDING_URL")
                or os.getenv("LLM_API_URL")
                or ""
            )
            try:
                reranker_top_n = int(os.getenv("RERANKER_TOP_N", "10"))
            except ValueError:
                reranker_top_n = 10
            
            if reranker_api_key and reranker_api_url:
                self.default_reranker_config = RerankerConfig(
                    api_url=reranker_api_url,
                    api_key=reranker_api_key,
                    model_name=env_reranker_model,
                    top_n=reranker_top_n
                )

    @staticmethod
    def _resolve_llm_key(config: LLMConfig) -> str:
        if config.api_key:
            return config.api_key
        return os.getenv("API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")

    async def retrieve_documents(
        self, 
        query: str, 
        collection_name: str,
        milvus_api_url: str,
        top_k: int = 10,
        score_threshold: float = 0.5,
        auth_header: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        从Milvus召回相关文档
        
        Returns:
            List of documents with format:
            {
                "id": int,
                "score": float,
                "chunk_text": str,
                "filename": str,
                "file_id": str,
                "metadata": dict,
                "created_at": str
            }
        """
        try:
            url = f"{milvus_api_url}/search"
            payload = {
                "collection_name": collection_name,
                "query_text": query,
                "top_k": top_k
            }

            print(f"正在从Milvus召回文档: {url}")
            headers = {}
            if auth_header:
                headers["Authorization"] = auth_header

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Milvus召回失败: {response.text}"
                )

            result = response.json()

            if result.get("status") != "success":
                raise HTTPException(
                    status_code=500,
                    detail=f"Milvus召回失败: {result}"
                )

            documents = result.get("results", [])

            # 过滤低于阈值的文档
            filtered_docs = [
                doc for doc in documents
                if doc.get("vector_score", doc.get("score", 0.0)) >= score_threshold
            ]

            print(f"✓ 召回 {len(documents)} 个文档，过滤后保留 {len(filtered_docs)} 个")
            return filtered_docs

        except httpx.RequestError as e:
            raise HTTPException(
                status_code=500,
                detail=f"调用Milvus API失败: {str(e)}"
            )
    
    async def rerank_documents(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        reranker_config: RerankerConfig
    ) -> List[Dict[str, Any]]:
        """
        使用重排序模型对文档进行重排序
        自动识别并适配不同的重排序服务：
        - BGE (BAAI): bge-reranker-*
        - 千问/阿里云: gte-rerank-*
        - Jina AI: jina-reranker-*
        """
        try:
            import httpx
            
            rerank_start = time.time()
            model_name = reranker_config.model_name.lower()
            print(f"正在使用重排序模型: {reranker_config.model_name}")
            
            # 准备文档文本列表
            doc_texts = [doc["chunk_text"] for doc in documents]
            
            # 根据模型名称自动识别服务类型
            if "jina" in model_name:
                # ============ Jina AI 重排序 ============
                rerank_payload = {
                    "model": reranker_config.model_name,
                    "query": query,
                    "documents": doc_texts,
                    "top_n": reranker_config.top_n
                }
                headers = {
                    "Authorization": f"Bearer {reranker_config.api_key}",
                    "Content-Type": "application/json"
                }
                rerank_url = f"{reranker_config.api_url.rstrip('/')}/rerank"
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        rerank_url,
                        json=rerank_payload,
                        headers=headers
                    )
                    response.raise_for_status()
                    result = response.json()
                
                # 解析 Jina 格式: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
                reranked_docs = []
                for item in result.get("results", []):
                    idx = item["index"]
                    doc = documents[idx].copy()
                    doc["retrieval_score"] = doc["score"]
                    doc["rerank_score"] = item["relevance_score"]
                    doc["score"] = doc["rerank_score"]
                    reranked_docs.append(doc)
            
            elif "qwen3-rerank" in model_name:
                rerank_payload = {
                    "model": reranker_config.model_name,
                    "query": query,
                    "documents": doc_texts,
                    "top_n": reranker_config.top_n,
                    "return_documents": False
                }
                headers = {
                    "Authorization": f"Bearer {reranker_config.api_key}",
                    "Content-Type": "application/json"
                }
                rerank_url = reranker_config.api_url.rstrip('/')
                if "{model}" in rerank_url:
                    rerank_url = rerank_url.replace("{model}", reranker_config.model_name)
                elif rerank_url.endswith("/compatible-mode/v1"):
                    base_url = rerank_url[:-len("/compatible-mode/v1")]
                    rerank_url = f"{base_url}/api/v1/services/rerank/{reranker_config.model_name}"
                elif "/services/" not in rerank_url:
                    rerank_url = f"{rerank_url}/api/v1/services/rerank/{reranker_config.model_name}"
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        rerank_url,
                        json=rerank_payload,
                        headers=headers
                    )
                    response.raise_for_status()
                    result = response.json()
                
                dashscope_results = result.get("output", {}).get("results", [])
                if not dashscope_results:
                    raise ValueError(f"qwen3重排序响应格式错误: {result}")
                
                reranked_docs = []
                for item in dashscope_results:
                    idx = item.get("index")
                    if idx is None:
                        doc_text = item.get("document", "")
                        if doc_text in doc_texts:
                            idx = doc_texts.index(doc_text)
                    if idx is None or idx >= len(documents):
                        continue
                    doc = documents[idx].copy()
                    doc["retrieval_score"] = doc["score"]
                    doc["rerank_score"] = item.get("score", item.get("relevance_score"))
                    doc["score"] = doc["rerank_score"]
                    reranked_docs.append(doc)
                
                if not reranked_docs:
                    raise ValueError(f"qwen3重排序返回为空: {result}")
                reranked_docs = reranked_docs[:reranker_config.top_n]
            
            elif "gte-rerank" in model_name or "dashscope" in reranker_config.api_url:
                # ============ 千问/阿里云 重排序 ============
                rerank_payload = {
                    "model": reranker_config.model_name,
                    "input": {
                        "query": query,
                        "documents": doc_texts
                    },
                    "parameters": {
                        "return_documents": False,
                        "top_n": reranker_config.top_n
                    }
                }
                headers = {
                    "Authorization": f"Bearer {reranker_config.api_key}",
                    "Content-Type": "application/json"
                }
                # 移除 /compatible-mode/v1 后缀
                base_url = reranker_config.api_url.replace("/compatible-mode/v1", "").rstrip('/')
                rerank_url = f"{base_url}/services/embeddings/text-embedding/text-rerank"
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        rerank_url,
                        json=rerank_payload,
                        headers=headers
                    )
                    response.raise_for_status()
                    result = response.json()
                
                # 解析千问格式: {"output": {"results": [{"index": 0, "relevance_score": 0.95}, ...]}}
                if "output" in result and "results" in result["output"]:
                    index_to_score = {
                        item["index"]: item["relevance_score"]
                        for item in result["output"]["results"]
                    }
                    
                    reranked_docs = []
                    for idx in sorted(index_to_score.keys(), 
                                    key=lambda x: index_to_score[x], 
                                    reverse=True)[:reranker_config.top_n]:
                        doc = documents[idx].copy()
                        doc["retrieval_score"] = doc["score"]
                        doc["rerank_score"] = index_to_score[idx]
                        doc["score"] = doc["rerank_score"]
                        reranked_docs.append(doc)
                else:
                    raise ValueError(f"千问重排序响应格式错误: {result}")
            
            elif "bge-reranker" in model_name:
                # ============ BGE/BAAI 重排序 ============
                # BGE 模型可能通过 OpenAI 兼容接口或自定义接口调用
                
                # 方式1: 如果使用 OpenAI 兼容接口（推荐）
                if "openai" in reranker_config.api_url or "v1" in reranker_config.api_url:
                    from openai import AsyncOpenAI
                    
                    client = AsyncOpenAI(
                        api_key=reranker_config.api_key,
                        base_url=reranker_config.api_url
                    )
                    
                    # 构造重排序请求（使用 embeddings 接口的扩展）
                    response = await client.post(
                        "/rerank",
                        json={
                            "model": reranker_config.model_name,
                            "query": query,
                            "documents": doc_texts,
                            "top_n": reranker_config.top_n
                        }
                    )
                    result = response.json()
                    
                    # 解析标准格式
                    reranked_docs = []
                    for item in result.get("results", []):
                        idx = item["index"]
                        doc = documents[idx].copy()
                        doc["retrieval_score"] = doc["score"]
                        doc["rerank_score"] = item.get("score", item.get("relevance_score"))
                        doc["score"] = doc["rerank_score"]
                        reranked_docs.append(doc)
                
                # 方式2: 使用原生 BGE 接口
                else:
                    rerank_payload = {
                        "query": query,
                        "passages": doc_texts,
                        "top_n": reranker_config.top_n
                    }
                    headers = {
                        "Authorization": f"Bearer {reranker_config.api_key}",
                        "Content-Type": "application/json"
                    }
                    rerank_url = f"{reranker_config.api_url.rstrip('/')}/rerank"
                    
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.post(
                            rerank_url,
                            json=rerank_payload,
                            headers=headers
                        )
                        response.raise_for_status()
                        result = response.json()
                    
                    # 解析 BGE 格式: {"scores": [0.95, 0.89, ...], "indices": [0, 5, ...]}
                    if "scores" in result and "indices" in result:
                        reranked_docs = []
                        for idx, score in zip(result["indices"], result["scores"]):
                            doc = documents[idx].copy()
                            doc["retrieval_score"] = doc["score"]
                            doc["rerank_score"] = score
                            doc["score"] = doc["rerank_score"]
                            reranked_docs.append(doc)
                    else:
                        raise ValueError(f"BGE重排序响应格式错误: {result}")
            
            else:
                # ============ 通用格式（尝试自动适配） ============
                print(f"⚠️ 未识别的模型类型，尝试通用格式")
                
                rerank_payload = {
                    "model": reranker_config.model_name,
                    "query": query,
                    "documents": doc_texts,
                    "top_n": reranker_config.top_n
                }
                headers = {
                    "Authorization": f"Bearer {reranker_config.api_key}",
                    "Content-Type": "application/json"
                }
                rerank_url = f"{reranker_config.api_url.rstrip('/')}/rerank"
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        rerank_url,
                        json=rerank_payload,
                        headers=headers
                    )
                    response.raise_for_status()
                    result = response.json()
                
                # 尝试解析常见格式
                reranked_docs = []
                if "results" in result:
                    # Jina/标准格式
                    for item in result["results"]:
                        idx = item["index"]
                        doc = documents[idx].copy()
                        doc["retrieval_score"] = doc["score"]
                        doc["rerank_score"] = item.get("relevance_score", item.get("score"))
                        doc["score"] = doc["rerank_score"]
                        reranked_docs.append(doc)
                else:
                    raise ValueError(f"无法解析重排序响应: {result}")
            
            rerank_time = time.time() - rerank_start
            print(f"✓ 重排序完成，保留 {len(reranked_docs)} 个文档 (耗时: {rerank_time:.2f}秒)")
            
            return reranked_docs
            
        except Exception as e:
            import traceback
            print(f"⚠️ 重排序失败: {str(e)}")
            print(f"⚠️ 错误详情:\n{traceback.format_exc()}")
            print(f"⚠️ 降级使用原始召回排序")
            
            # 重排序失败时，保留原始排序的前 top_n 个文档
            fallback_docs = []
            for doc in documents[:reranker_config.top_n]:
                doc_copy = doc.copy()
                doc_copy["retrieval_score"] = doc["score"]
                doc_copy["rerank_score"] = None
                fallback_docs.append(doc_copy)
            return fallback_docs
    
    def format_context(self, documents: List[Dict[str, Any]]) -> str:
        """
        格式化文档为上下文字符串，包含图片引用
        """
        context_parts = []

        for i, doc in enumerate(documents):
            filename = doc.get("filename", "未知文件")
            text = doc.get("chunk_text", "")
            score = doc.get("score", 0.0)

            # 提取metadata中的页码信息和图片引用（如果有）
            metadata = doc.get("metadata", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except:
                    metadata = {}

            page_info = ""
            if "page_start" in metadata:
                page_start = metadata["page_start"]
                page_end = metadata.get("page_end", page_start)
                if page_start == page_end:
                    page_info = f"(第{page_start}页)"
                else:
                    page_info = f"(第{page_start}-{page_end}页)"

            # 构建片段信息
            part_text = f"[文档片段 {i+1}] 来源: {filename}{page_info} | 相关度: {score:.3f}\n{text}"

            # 如果metadata中包含图片引用，添加到上下文中
            images = metadata.get("images", [])
            if images:
                image_refs = []
                for img in images:
                    img_url = img.get("url", "")
                    img_alt = img.get("alt_text", img.get("filename", "图片"))
                    if img_url:
                        # 保持相对路径（如 /document/xxx/images/yyy.png）
                        # 前端会根据部署环境拼接正确的 URL 前缀
                        image_refs.append(f"![{img_alt}]({img_url})")

                if image_refs:
                    part_text += "\n\n**相关图片（可直接引用到回答中）**:\n" + "\n".join(image_refs)

            context_parts.append(part_text)

        return "\n\n".join(context_parts)

    @staticmethod
    def _extract_reasoning(delta: Any) -> Optional[str]:
        """从流式响应中提取思考内容（OpenRouter格式）"""
        for attr in ("reasoning_content", "reasoning"):
            value = getattr(delta, attr, None)
            if not value:
                continue
            if isinstance(value, dict):
                text = value.get("content") or value.get("text") or value.get("details")
                if text:
                    return str(text)
            if isinstance(value, str):
                return value
        return None

    async def call_llm_stream(
        self,
        messages: List[Dict[str, str]],
        llm_config: LLMConfig,
        enable_thinking: bool = False
    ) -> AsyncIterable[Dict[str, Any]]:
        """
        流式调用大模型，返回reasoning和content事件
        """
        try:
            api_key = self._resolve_llm_key(llm_config)
            if not api_key:
                raise HTTPException(
                    status_code=500,
                    detail="调用LLM失败: 未配置模型访问密钥",
                )
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=llm_config.api_url
            )

            # 构建请求参数
            request_kwargs = {
                "model": llm_config.model_name,
                "messages": messages,
                "temperature": llm_config.temperature,
                "max_tokens": llm_config.max_tokens,
                "stream": True
            }

            # 如果启用思考模式，添加extra_body参数
            if enable_thinking:
                if "deepseek" in llm_config.api_url.lower():
                    # DeepSeek 使用 thinking 参数
                    request_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    print("✓ 已启用 DeepSeek 思考模式")
                elif "openrouter" in llm_config.api_url.lower():
                    # OpenRouter 使用 reasoning 参数 (legacy, kept for transition)
                    request_kwargs["extra_body"] = {"reasoning": {"enabled": True}}
                    print("✓ 已启用 OpenRouter 思考模式")
                else:
                    # DashScope 使用 enable_thinking 参数
                    request_kwargs["extra_body"] = {"enable_thinking": True, "thinking_budget": 81920}
                    print("✓ 已启用 DashScope 思考模式")

            stream = await client.chat.completions.create(**request_kwargs)

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # 提取思考内容（OpenRouter格式）
                reasoning = self._extract_reasoning(delta)
                if reasoning:
                    yield {"type": "reasoning", "data": reasoning}

                # 提取回答内容
                content = getattr(delta, "content", None)
                if content:
                    yield {"type": "content", "data": content}
                    
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"调用LLM失败: {str(e)}"
            )
    
    async def call_llm_non_stream(
        self,
        messages: List[Dict[str, str]],
        llm_config: LLMConfig
    ) -> str:
        """
        非流式调用大模型
        """
        try:
            api_key = self._resolve_llm_key(llm_config)
            if not api_key:
                raise HTTPException(
                    status_code=500,
                    detail="调用LLM失败: 未配置模型访问密钥",
                )
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=llm_config.api_url
            )
            
            response = await client.chat.completions.create(
                model=llm_config.model_name,
                messages=messages,
                temperature=llm_config.temperature,
                max_tokens=llm_config.max_tokens,
                stream=False
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"调用LLM失败: {str(e)}"
            )
    
    async def chat_stream(
        self,
        request: ChatRequest,
        auth_header: Optional[str] = None,
    ) -> AsyncIterable[str]:
        """
        流式对话处理
        """
        try:
            start_time = time.time()
            
            # 1. 召回文档
          
            retrieve_start = time.time()
            documents = await self.retrieve_documents(
                query=request.query,
                collection_name=request.collection_name,
                milvus_api_url=request.milvus_api_url,
                top_k=request.top_k,
                score_threshold=request.score_threshold,
                auth_header=auth_header,
            )
            retrieve_time = time.time() - retrieve_start
            
            if not documents:
                # 没有找到相关文档，直接用LLM回答
                print("⚠️ 未找到相关文档，使用LLM直接回答")
                messages = []
                
                # 添加历史对话
                for msg in request.history:
                    messages.append({
                        "role": msg.role,
                        "content": msg.content
                    })
                
                # 添加当前问题
                messages.append({
                    "role": "user",
                    "content": request.query
                })
                
                # 流式返回
                async for event in self.call_llm_stream(messages, request.llm_config, request.enable_thinking):
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                
                # 返回元数据
                yield json.dumps({
                    "type": "metadata",
                    "data": {
                        "retrieve_time": retrieve_time,
                        "total_time": time.time() - start_time,
                        "documents_count": 0
                    }
                }, ensure_ascii=False) + "\n"
                return
            
            # 2. 重排序（可选）
            reranker_cfg = request.reranker_config or self.default_reranker_config
            if request.use_reranker and reranker_cfg:
                cfg_data = reranker_cfg.dict()
                cfg_data["top_n"] = max(1, min(cfg_data.get("top_n", len(documents)), len(documents)))
                reranker_config = RerankerConfig(**cfg_data)
                rerank_start = time.time()
                documents = await self.rerank_documents(
                    query=request.query,
                    documents=documents,
                    reranker_config=reranker_config
                )
                rerank_time = time.time() - rerank_start
                print(f"✓ 重排序耗时: {rerank_time:.2f}秒")
            else:
                rerank_time = 0
            
            # 3. 构建上下文
            context = self.format_context(documents)
            
            # 4. 构建prompt
            prompt_template = request.prompt_template or self.default_prompt_template
            user_message = prompt_template.format(
                context=context,
                query=request.query
            )
            
            # 5. 构建消息列表
            messages = []

            # 检测并应用苏格拉底模式
            socratic_enabled = request.socratic_mode or detect_socratic_mode(request.query)
            if socratic_enabled:
                socratic_prompt = await load_econ_prompts("socratic_method")
                if socratic_prompt:
                    messages.append({
                        "role": "system",
                        "content": socratic_prompt
                    })
                    print("✓ 已启用苏格拉底问答法模式")

            # 添加历史对话
            for msg in request.history:
                messages.append({
                    "role": msg.role,
                    "content": msg.content
                })
            
            # 添加当前问题
            messages.append({
                "role": "user",
                "content": user_message
            })
            
            # 6. 调用LLM（流式）
            llm_start = time.time()
            async for event in self.call_llm_stream(messages, request.llm_config, request.enable_thinking):
                yield json.dumps(event, ensure_ascii=False) + "\n"
            llm_time = time.time() - llm_start
            
            # 7. 发送来源文档
            if request.return_source and documents:
                sources = []
                for doc in documents:
                    sources.append({
                        "chunk_text": doc["chunk_text"],
                        "filename": doc["filename"],
                        "score": doc["score"],
                        "retrieval_score": doc.get("retrieval_score"),
                        "rerank_score": doc.get("rerank_score"),
                        "metadata": doc.get("metadata", {})
                    })
                
                yield json.dumps({
                    "type": "sources",
                    "data": sources
                }, ensure_ascii=False) + "\n"
            
            # 8. 返回元数据
            total_time = time.time() - start_time
            yield json.dumps({
                "type": "metadata",
                "data": {
                    "retrieve_time": retrieve_time,
                    "rerank_time": rerank_time,
                    "llm_time": llm_time,
                    "total_time": total_time,
                    "documents_count": len(documents)
                }
            }, ensure_ascii=False) + "\n"
            
            print(f"\n{'='*60}")
            print(f"✓ RAG对话完成")
            print(f"  - 召回耗时: {retrieve_time:.2f}秒")
            print(f"  - 重排序耗时: {rerank_time:.2f}秒")
            print(f"  - LLM耗时: {llm_time:.2f}秒")
            print(f"  - 总耗时: {total_time:.2f}秒")
            print(f"  - 文档数量: {len(documents)}")
            print(f"{'='*60}\n")
            
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"❌ RAG对话失败: {error_trace}")
            yield json.dumps({
                "type": "error",
                "data": {
                    "error": str(e),
                    "traceback": error_trace
                }
            }, ensure_ascii=False) + "\n"
    
    async def chat_non_stream(
        self,
        request: ChatRequest,
        auth_header: Optional[str] = None,
    ) -> ChatResponse:
        """
        非流式对话处理
        """
        start_time = time.time()
        
        try:
            # 1. 召回文档
            print(f"\n{'='*60}")
            print(f"开始RAG对话流程（非流式）")
            print(f"{'='*60}\n")
            
            retrieve_start = time.time()
            documents = await self.retrieve_documents(
                query=request.query,
                collection_name=request.collection_name,
                milvus_api_url=request.milvus_api_url,
                top_k=request.top_k,
                score_threshold=request.score_threshold,
                auth_header=auth_header,
            )
            retrieve_time = time.time() - retrieve_start
            
            if not documents:
                # 没有找到相关文档，直接用LLM回答
                print("⚠️ 未找到相关文档，使用LLM直接回答")
                messages = []
                
                for msg in request.history:
                    messages.append({
                        "role": msg.role,
                        "content": msg.content
                    })
                
                messages.append({
                    "role": "user",
                    "content": request.query
                })
                
                answer = await self.call_llm_non_stream(messages, request.llm_config)
                
                return ChatResponse(
                    success=True,
                    message="对话完成（未找到相关文档）",
                    answer=answer,
                    sources=None,
                    metadata={
                        "retrieve_time": retrieve_time,
                        "total_time": time.time() - start_time,
                        "documents_count": 0
                    }
                )
            
            # 2. 重排序（可选）
            reranker_cfg = request.reranker_config or self.default_reranker_config
            if request.use_reranker and reranker_cfg:
                cfg_data = reranker_cfg.dict()
                cfg_data["top_n"] = max(1, min(cfg_data.get("top_n", len(documents)), len(documents)))
                reranker_config = RerankerConfig(**cfg_data)
                rerank_start = time.time()
                documents = await self.rerank_documents(
                    query=request.query,
                    documents=documents,
                    reranker_config=reranker_config
                )
                rerank_time = time.time() - rerank_start
            else:
                rerank_time = 0
            
            # 3. 构建上下文
            context = self.format_context(documents)
            
            # 4. 构建prompt
            prompt_template = request.prompt_template or self.default_prompt_template
            user_message = prompt_template.format(
                context=context,
                query=request.query
            )
            
            # 5. 构建消息列表
            messages = []

            # 检测并应用苏格拉底模式
            socratic_enabled = request.socratic_mode or detect_socratic_mode(request.query)
            if socratic_enabled:
                socratic_prompt = await load_econ_prompts("socratic_method")
                if socratic_prompt:
                    messages.append({
                        "role": "system",
                        "content": socratic_prompt
                    })
                    print("✓ 已启用苏格拉底问答法模式")

            for msg in request.history:
                messages.append({
                    "role": msg.role,
                    "content": msg.content
                })
            messages.append({
                "role": "user",
                "content": user_message
            })
            
            # 6. 调用LLM（非流式）
            llm_start = time.time()
            answer = await self.call_llm_non_stream(messages, request.llm_config)
            llm_time = time.time() - llm_start
            
            # 7. 构建来源文档
            sources = None
            if request.return_source:
                sources = []
                for doc in documents:
                    source_doc = SourceDocument(
                        chunk_text=doc["chunk_text"],
                        filename=doc["filename"],
                        score=doc["score"],  # 主分数
                        retrieval_score=doc.get("retrieval_score"),  # 原始召回分数
                        rerank_score=doc.get("rerank_score"),  # 重排序分数
                        metadata=doc.get("metadata", {})
                    )
                    sources.append(source_doc)
            
            # 8. 返回结果
            total_time = time.time() - start_time
            
            print(f"\n{'='*60}")
            print(f"✓ RAG对话完成")
            print(f"  - 召回耗时: {retrieve_time:.2f}秒")
            print(f"  - 重排序耗时: {rerank_time:.2f}秒")
            print(f"  - LLM耗时: {llm_time:.2f}秒")
            print(f"  - 总耗时: {total_time:.2f}秒")
            print(f"  - 文档数量: {len(documents)}")
            print(f"{'='*60}\n")
            
            return ChatResponse(
                success=True,
                message="对话完成",
                answer=answer,
                sources=sources,
                metadata={
                    "retrieve_time": retrieve_time,
                    "rerank_time": rerank_time,
                    "llm_time": llm_time,
                    "total_time": total_time,
                    "documents_count": len(documents)
                }
            )
            
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"❌ RAG对话失败: {error_trace}")
            raise HTTPException(
                status_code=500,
                detail=f"对话失败: {str(e)}"
            )

# ============ FastAPI应用 ============

app = FastAPI(
    title="RAG对话服务API",
    description="支持向量召回、重排序、流式/非流式问答",
    version="1.0.0",
    dependencies=[Depends(verify_jwt)],
)

# 添加CORS支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

service = ChatService()
qa_service = QAService()
try:
    QA_RETENTION_DAYS = int(os.getenv("QA_RETENTION_DAYS", "28"))
except ValueError:
    QA_RETENTION_DAYS = 28
qa_logger = logging.getLogger("qa")


def _dump_qa_event(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"

@app.get("/")
async def root():
    """健康检查"""
    return {
        "status": "running",
        "service": "RAG Chat API",
        "version": "1.0.0",
        "features": ["vector_retrieval", "reranking", "streaming", "non_streaming"]
    }

@app.post("/chat")
async def chat(request: ChatRequest, http_request: Request):
    """
    RAG对话接口
    
    支持流式和非流式两种模式：
    
    **流式模式** (stream=True):
    返回格式为 NDJSON (换行分隔的JSON)，每行是一个事件：
    - {"type": "content", "data": "..."} - 内容片段
    - {"type": "sources", "data": [...]} - 来源文档（可选）
    - {"type": "metadata", "data": {...}} - 元数据
    - {"type": "error", "data": {...}} - 错误信息
    
    **非流式模式** (stream=False):
    返回完整的JSON响应
    """
    try:
        auth_header = http_request.headers.get("Authorization")
        if request.stream:
            # 流式返回
            return StreamingResponse(
                service.chat_stream(request, auth_header=auth_header),
                media_type="application/x-ndjson"
            )
        else:
            # 非流式返回
            return await service.chat_non_stream(request, auth_header=auth_header)
            
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"对话失败: {str(e)}"
        )

@app.get("/qa/models")
async def get_qa_models():
    return {
        "status": "success",
        "models": qa_service.list_models(),
    }


@app.post("/qa/sessions")
async def create_qa_session_endpoint(request: QASessionCreate, user_id: str = Depends(verify_jwt)):
    await purge_old_sessions(QA_RETENTION_DAYS)
    session = await create_session(user_id, request.title, request.model_name)
    return {"status": "success", "session": session}


@app.get("/qa/sessions")
async def list_qa_sessions_endpoint(user_id: str = Depends(verify_jwt)):
    await purge_old_sessions(QA_RETENTION_DAYS)
    sessions = await list_sessions(user_id)
    return {"status": "success", "sessions": sessions}


@app.get("/qa/sessions/{session_id}")
async def get_qa_session_endpoint(session_id: str, user_id: str = Depends(verify_jwt)):
    session = await get_session(user_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = await get_messages(session_id)
    for message in messages:
        # 处理 images 字段：确保返回列表类型
        images = message.get("images")
        if images is not None and not isinstance(images, list):
            try:
                message["images"] = json.loads(images) if isinstance(images, str) else []
            except (json.JSONDecodeError, TypeError):
                message["images"] = []

        # 处理 files 字段
        files = message.get("files")
        if isinstance(files, list):
            message["files"] = [
                {"filename": file.get("filename", "document.pdf")}
                for file in files
                if isinstance(file, dict)
            ]
    return {"status": "success", "session": session, "messages": messages}


@app.delete("/qa/sessions/{session_id}")
async def delete_qa_session_endpoint(session_id: str, user_id: str = Depends(verify_jwt)):
    deleted = await delete_session(user_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"status": "success"}


@app.post("/qa/chat")
async def qa_chat(request: QARequest, user_id: str = Depends(verify_jwt)):
    request_start = time.time()
    await purge_old_sessions(QA_RETENTION_DAYS)
    model = qa_service.get_model(request.model_key)
    if not model:
        raise HTTPException(status_code=400, detail="未知模型")

    if request.files and not model.supports_pdf:
        raise HTTPException(status_code=400, detail="当前模型不支持 PDF")

    session = None
    session_id = request.session_id
    if session_id:
        session = await get_session(user_id, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
    else:
        title = request.query.strip()[:50] or "未命名会话"
        session = await create_session(user_id, title, request.model_key)
        session_id = session.get("session_id")

    qa_logger.info(
        "QA request user=%s model=%s session=%s images=%d files=%d thinking=%s",
        user_id,
        request.model_key,
        session_id,
        len(request.images or []),
        len(request.files or []),
        request.enable_thinking,
    )

    history = await get_messages(session_id)
    if len(history) > 20:
        history = history[-20:]

    messages = qa_service.build_messages(history, provider=model.provider)

    # 经济答题区：注入系统提示词
    if request.question_type:
        system_prompt = await load_econ_prompts(request.question_type)
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
            qa_logger.info("Injected econ prompt for question_type=%s", request.question_type)

    file_payloads: Optional[List[Dict[str, Any]]] = None
    if request.files:
        file_payloads = []
        for file in request.files:
            payload = file.dict() if hasattr(file, "dict") else file.model_dump()
            file_payloads.append(payload)

    messages.append({
        "role": "user",
        "content": qa_service.build_content(
            request.query,
            images=request.images,
            files=file_payloads,
        ),
    })

    await insert_message(
        session_id=session_id,
        role="user",
        content=request.query,
        images=request.images,
        files=file_payloads,
        metadata={"model_key": request.model_key},
    )

    if session and not session.get("title"):
        await touch_session(session_id, title=request.query.strip()[:50], model_name=request.model_key)
    else:
        await touch_session(session_id, model_name=request.model_key)

    async def stream_response() -> AsyncIterable[str]:
        assistant_content = ""
        assistant_reasoning = ""

        if request.session_id is None and session:
            yield _dump_qa_event({
                "type": "session",
                "data": session,
            })

        try:
            async for event in qa_service.chat_stream(
                request.model_key,
                messages,
                enable_thinking=request.enable_thinking,
            ):
                if event.get("type") == "content":
                    assistant_content += event.get("data", "")
                elif event.get("type") == "reasoning":
                    assistant_reasoning += event.get("data", "")
                yield _dump_qa_event(event)
        except Exception as exc:
            qa_logger.exception("QA stream failed session=%s", session_id)
            yield _dump_qa_event({
                "type": "error",
                "data": {"error": str(exc)},
            })
            return

        if assistant_content or assistant_reasoning:
            await insert_message(
                session_id=session_id,
                role="assistant",
                content=assistant_content,
                reasoning_content=assistant_reasoning or None,
                metadata={"model_key": request.model_key},
            )
            await touch_session(session_id)
            qa_logger.info(
                "QA completed session=%s elapsed=%.2fs",
                session_id,
                time.time() - request_start,
            )

        yield _dump_qa_event({"type": "end"})

    return StreamingResponse(stream_response(), media_type="application/x-ndjson")

@app.get("/health")
async def health_check():
    """服务健康检查"""
    return {
        "status": "healthy",
        "service": "rag-chat",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/config/default")
async def get_default_config():
    """获取默认的LLM配置"""
    from pathlib import Path
    from dotenv import load_dotenv

    # 加载 backend/.env 文件
    env_path = Path(__file__).parent.parent / '.env'
    load_dotenv(dotenv_path=env_path, override=True)

    deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    dashscope_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    return {
        "status": "success",
        "config": {
            "llm": {
                "api_url": os.getenv("MODEL_URL", deepseek_base_url),
                "api_key": os.getenv("API_KEY", ""),
                "model_name": os.getenv("CHAT_DEFAULT_MODEL", "deepseek-v4-flash"),
                "temperature": 0.7,
                "max_tokens": 5000
            },
            "retrieval": {
                "top_k": 10,
                "score_threshold": 0.15
            },
            "available_models": [
                # ===== DeepSeek 模型 =====
                {"name": "deepseek-v4-flash", "display": "DeepSeek V4 Flash", "provider": "deepseek",
                 "api_url": deepseek_base_url, "supports_thinking": True, "supports_vision": False},
                {"name": "deepseek-v4-pro", "display": "DeepSeek V4 Pro", "provider": "deepseek",
                 "api_url": deepseek_base_url, "supports_thinking": True, "supports_vision": False},
                # ===== DashScope 阿里云模型 =====
                {"name": "kimi-k2.6", "display": "Kimi K2.6", "provider": "dashscope",
                 "api_url": dashscope_base_url, "supports_thinking": True, "supports_vision": True},
                {"name": "qwen3.7-max", "display": "通义千问 3.7 Max", "provider": "dashscope",
                 "api_url": dashscope_base_url, "supports_thinking": True, "supports_vision": False},
                {"name": "qwen3.7-plus", "display": "通义千问 3.7 Plus", "provider": "dashscope",
                 "api_url": dashscope_base_url, "supports_thinking": True, "supports_vision": True},
                {"name": "qwen3.6-35b-a3b", "display": "通义千问 3.6 35B A3B", "provider": "dashscope",
                 "api_url": dashscope_base_url, "supports_thinking": True, "supports_vision": True},
                {"name": "qwen3.6-27b", "display": "通义千问 3.6 27B", "provider": "dashscope",
                 "api_url": dashscope_base_url, "supports_thinking": True, "supports_vision": True},
            ],
            # DeepSeek API 配置
            "deepseek": {
                "api_url": deepseek_base_url,
                "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
            }
        }
    }

# ============ 启动服务 ============

if __name__ == "__main__":
    import os
    
    host = os.getenv("SERVER_HOST", SERVICE_HOST)
    port = int(os.getenv("SERVER_PORT", "8501"))
    
    print("\n" + "="*60)
    print("启动RAG对话服务")
    print("="*60)
    print(f"服务地址: http://{host}:{port}")
    print(f"API文档: http://{host}:{port}/docs")
    print("="*60 + "\n")
    
    uvicorn.run(
        app,
        host=SERVICE_HOST,
        port=SERVICE_PORT,
        log_level="info"
    )
