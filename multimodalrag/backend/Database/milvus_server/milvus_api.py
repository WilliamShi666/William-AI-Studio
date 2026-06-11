import uuid
import json
import os
import shutil
import requests
import time
import math
import re
from collections import Counter
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

import uvicorn
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pymilvus import connections, Collection, CollectionSchema, FieldSchema, DataType, utility
from dotenv import load_dotenv
import glob
import logging

# 加载 backend/.env 文件
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)

# 环境变量配置
MILVUS_HOST = os.getenv("MILVUS_HOST")
MILVUS_PORT = os.getenv("MILVUS_PORT")
EMBEDDING_URL = os.getenv("EMBEDDING_URL")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY")

# Chunking service (for v2 re-split) - reuse same env keys as extraction service
CHUNK_SERVICE_HOST = os.getenv("CHUNK_SERVICE_HOST", "localhost")
CHUNK_SERVICE_PORT = int(os.getenv("CHUNK_SERVICE_PORT", "8001"))
# If service binds 0.0.0.0, use localhost for HTTP
_chunk_host_for_url = "localhost" if CHUNK_SERVICE_HOST == "0.0.0.0" else CHUNK_SERVICE_HOST
CHUNKING_SERVICE_URL_V2 = f"http://{_chunk_host_for_url}:{CHUNK_SERVICE_PORT}/chunk/v2"

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
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}
ADMIN_ROLES = {"admin"}


def _is_public_path(path: str) -> bool:
    return (
        path in PUBLIC_PATHS
        or path.startswith("/docs")
        or (path.startswith("/document/") and "/images/" in path)
    )


def _require_jwt_secret() -> str:
    if not JWT_SECRET_KEY or JWT_SECRET_KEY in UNSAFE_JWT_SECRETS or len(JWT_SECRET_KEY) < 32:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY is not configured safely")
    return JWT_SECRET_KEY


async def verify_jwt(request: Request) -> str:
    if request.method == "OPTIONS" or _is_public_path(request.url.path):
        request.state.user_id = "anonymous"
        request.state.user_role = "anonymous"
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
    request.state.user_id = user_id
    request.state.user_role = payload.get("role") or "admin"
    return user_id


async def require_admin(request: Request) -> str:
    user_role = getattr(request.state, "user_role", None)
    if user_role is None:
        await verify_jwt(request)
        user_role = getattr(request.state, "user_role", None)
    if user_role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    return getattr(request.state, "user_id", "")

# Local storage dirs
EXTRACTION_RESULTS_DIR = Path(os.getenv(
    "EXTRACTION_RESULTS_DIR",
    "./output/extraction_results"
))

def _get_chunk_copy_logger() -> logging.Logger:
    logger = logging.getLogger("chunk_copy")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logs_dir = Path(__file__).resolve().parents[3] / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # If logs dir can't be created, fall back to stderr only
        pass

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    try:
        file_handler = logging.FileHandler(logs_dir / "chunk_copy.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception:
        # ignore file handler errors
        pass

    logger.propagate = False
    return logger

chunk_copy_logger = _get_chunk_copy_logger()

# Pydantic 模型定义
class DocumentChunk(BaseModel):
    chunk_text: str
    filename: str
    file_id: Optional[str] = None
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = {}

class UploadKBRequest(BaseModel):
    collection_name: str
    file_data: Dict[str, Any]

class UploadResponse(BaseModel):
    filename: str
    file_id: str
    status: str
    message: str
    chunks_count: int = 0

class SearchRequest(BaseModel):
    collection_name: str
    query_text: str
    top_k: int = 10
    filter_expr: Optional[str] = None

class SearchByFilenameRequest(BaseModel):
    collection_name: str
    filename: str
    top_k: int = 10

class DeleteRequest(BaseModel):
    collection_name: str
    filename: str


# New: delete document by file_id within a collection
class DeleteByIdRequest(BaseModel):
    collection_id: str
    file_id: str

class DeleteKBRequest(BaseModel):
    collection_name: str

class DeleteDocumentRequest(BaseModel):
    collection_id: str
    file_id: str
    filename: str

class CopyChunksRequest(BaseModel):
    source_collection_id: str
    target_collection_id: str
    # Accept strings to avoid JavaScript number precision loss (INT64 > MAX_SAFE_INTEGER)
    chunk_ids: List[str]
    new_file_id: Optional[str] = None
    new_filename: Optional[str] = None

class MilvusRAGService:
    def __init__(self):
        self.milvus_host = MILVUS_HOST
        self.milvus_port = MILVUS_PORT
        self.embedding_url = self._normalize_embedding_url(EMBEDDING_URL)
        self.embedding_model_name = EMBEDDING_MODEL_NAME
        self.embedding_api_key = EMBEDDING_API_KEY
        try:
            self.embedding_max_retries = int(os.getenv("EMBEDDING_MAX_RETRIES", "3"))
        except ValueError:
            self.embedding_max_retries = 3
        self.embedding_max_retries = max(self.embedding_max_retries, 1)
        try:
            self.embedding_retry_backoff = float(os.getenv("EMBEDDING_RETRY_BACKOFF", "0.5"))
        except ValueError:
            self.embedding_retry_backoff = 0.5
        self.embedding_retry_backoff = max(self.embedding_retry_backoff, 0.1)
        try:
            self.hybrid_dense_weight = float(os.getenv("HYBRID_DENSE_WEIGHT", "0.7"))
        except ValueError:
            self.hybrid_dense_weight = 0.7
        
        print(f"Milvus配置: {self.milvus_host}:{self.milvus_port}")
        print(f"Embedding配置: URL={self.embedding_url}, Model={self.embedding_model_name}")
        
        self.connect_to_milvus()

    def _normalize_embedding_url(self, url: Optional[str]) -> str:
        """确保Embedding URL指向实际的接口路径"""
        if not url:
            raise ValueError("未配置 EMBEDDING_URL")
        url = url.rstrip("/")
        if url.endswith("/embeddings"):
            return url
        if url.endswith("/compatible-mode/v1"):
            return f"{url}/embeddings"
        if url.endswith("/v1"):
            return f"{url}/embeddings"
        return f"{url}/embeddings"
    
    def connect_to_milvus(self):
        """连接到Milvus服务"""
        try:
            connections.connect(
                "default", 
                host=self.milvus_host, 
                port=self.milvus_port
            )
            print(f"Successfully connected to Milvus at {self.milvus_host}:{self.milvus_port}")
        except Exception as e:
            print(f"Failed to connect to Milvus: {e}")
            raise
    
    def _execute_embedding_request(self, inputs: List[str], timeout: float = 30.0) -> List[List[float]]:
        """调用Embedding服务，带重试"""
        if not inputs:
            return []
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.embedding_api_key}"
        }
        
        payload = {
            "model": self.embedding_model_name,
            "input": inputs
        }
        
        last_error: Optional[Exception] = None
        for attempt in range(1, self.embedding_max_retries + 1):
            try:
                response = requests.post(
                    self.embedding_url,
                    json=payload,
                    headers=headers,
                    timeout=timeout
                )
                if response.status_code != 200:
                    body_preview = response.text.strip()
                    if len(body_preview) > 800:
                        body_preview = body_preview[:800] + "...(truncated)"
                    raise RuntimeError(
                        f"Embedding API HTTP {response.status_code}: {body_preview}"
                    )
                
                try:
                    result = response.json()
                except ValueError as json_err:
                    raise RuntimeError(f"Embedding API 响应解析失败: {json_err}")
                
                data = result.get("data", [])
                if len(data) != len(inputs):
                    raise RuntimeError("Embedding API 响应数量与输入不匹配")
                
                embeddings = [item["embedding"] for item in data]
                return embeddings
            except Exception as exc:
                last_error = exc
                wait_time = min(self.embedding_retry_backoff * (2 ** (attempt - 1)), 5.0)
                print(f"Embedding API 调用失败 (第 {attempt} 次): {exc}. {wait_time:.1f}s 后重试...")
                time.sleep(wait_time)
        
        raise RuntimeError(f"Embedding API 多次调用失败: {last_error}")
    
    def generate_embedding(self, text: str) -> List[float]:
        """单个文本生成向量（用于query）"""
        vectors = self._execute_embedding_request([text], timeout=30.0)
        return vectors[0]
    
    def generate_embeddings_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """
        批量生成embeddings
        """
        if not texts:
            return []
        
        total_texts = len(texts)
        print(f"开始批量生成 {total_texts} 个文本的embeddings，批次大小: {batch_size}")
        
        all_embeddings: List[List[float]] = []
        start_time = time.time()
        
        for batch_start in range(0, total_texts, batch_size):
            batch_texts = texts[batch_start:batch_start + batch_size]
            batch_embeddings = self._execute_embedding_request(batch_texts, timeout=60.0)
            all_embeddings.extend(batch_embeddings)
        
        elapsed = time.time() - start_time
        rate = total_texts / elapsed if elapsed > 0 else 0
        print(f"批量生成完成，总耗时: {elapsed:.2f}秒，平均速率: {rate:.2f} 文本/秒")
        
        return all_embeddings
    
    def get_model_dimension(self, model_name: str) -> int:
        """获取模型的向量维度"""
        model_dimensions = {
            "jina-embeddings-v4": 2048,
            "jina-embeddings-v3": 1024,
            "jina-embeddings-v2-base-zh": 512,
            "text-embedding-ada-002": 1536,
            "text-embedding-v4": 1024
        }
        return model_dimensions.get(model_name, 1024)
    
    def _tokenize(self, text: str) -> List[str]:
        """简单分词，支持中英文"""
        if not text:
            return []
        return re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]+", text.lower())
    
    def _apply_hybrid_scores(self, query_text: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将稠密分数与简易BM25稀疏分数融合"""
        if not results:
            return results
        
        query_tokens = self._tokenize(query_text)
        if not query_tokens:
            for item in results:
                item["vector_score"] = float(item.get("score", 0.0))
                item["lexical_score"] = 0.0
            return results
        
        doc_tokens: List[List[str]] = []
        df = Counter()
        for item in results:
            tokens = self._tokenize(item.get("chunk_text", ""))
            doc_tokens.append(tokens)
            for term in set(tokens):
                df[term] += 1
        
        avgdl = sum(len(tokens) for tokens in doc_tokens) / max(len(doc_tokens), 1)
        k1 = 1.6
        b = 0.75
        N = len(doc_tokens)
        
        lexical_scores: List[float] = []
        for tokens in doc_tokens:
            freq = Counter(tokens)
            score = 0.0
            for term in query_tokens:
                tf = freq.get(term, 0)
                if not tf:
                    continue
                df_term = df.get(term, 0) or 1
                idf = math.log((N - df_term + 0.5) / (df_term + 0.5) + 1)
                denom = tf + k1 * (1 - b + b * len(tokens) / (avgdl or 1))
                score += idf * tf * (k1 + 1) / denom
            lexical_scores.append(score)
        
        max_lex = max(lexical_scores) if lexical_scores else 0.0
        alpha = min(max(self.hybrid_dense_weight, 0.0), 1.0)
        
        for item, lex_score in zip(results, lexical_scores):
            dense_score = float(item.get("score", 0.0))
            normalized = 0.0
            if max_lex > 0:
                normalized = lex_score / max_lex
            
            combined = alpha * dense_score + (1 - alpha) * normalized
            item["vector_score"] = dense_score
            item["lexical_score"] = normalized
            item["score"] = combined
        
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
    
    def create_collection_schema(self, embedding_dim: int = 1024) -> CollectionSchema:
        """创建Collection的Schema"""
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="chunk_text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=255),
            FieldSchema(name="file_id", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=embedding_dim),
            FieldSchema(name="metadata", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="created_at", dtype=DataType.VARCHAR, max_length=50)
        ]
        
        schema = CollectionSchema(
            fields=fields,
            description="RAG Knowledge Base Collection"
        )
        return schema
    
    def generate_collection_id(self) -> str:
        """生成符合Milvus规范的collection ID"""
        import time
        timestamp = int(time.time() * 1000)  # 毫秒级时间戳
        return f"kb_{timestamp}"

    def save_name_mapping(self, collection_id: str, display_name: str):
        """保存collection ID和显示名称的映射关系到JSON文件"""
        import json
        from pathlib import Path

        # 映射文件路径
        mapping_file = Path(__file__).parent / "collection_name_mapping.json"

        # 读取现有映射
        mappings = {}
        if mapping_file.exists():
            with open(mapping_file, 'r', encoding='utf-8') as f:
                mappings = json.load(f)

        # 更新映射
        mappings[collection_id] = {
            "display_name": display_name,
            "created_at": datetime.now().isoformat()
        }

        # 保存映射
        with open(mapping_file, 'w', encoding='utf-8') as f:
            json.dump(mappings, f, ensure_ascii=False, indent=2)

        print(f"Saved mapping: {collection_id} -> {display_name}")

    def get_name_mapping(self, collection_id: str = None) -> Dict[str, Any]:
        """获取名称映射"""
        import json
        from pathlib import Path

        mapping_file = Path(__file__).parent / "collection_name_mapping.json"

        if not mapping_file.exists():
            return {}

        with open(mapping_file, 'r', encoding='utf-8') as f:
            mappings = json.load(f)

        if collection_id:
            return mappings.get(collection_id, {})
        return mappings

    def delete_name_mapping(self, collection_id: str):
        """删除名称映射"""
        import json
        from pathlib import Path

        mapping_file = Path(__file__).parent / "collection_name_mapping.json"

        if not mapping_file.exists():
            return

        with open(mapping_file, 'r', encoding='utf-8') as f:
            mappings = json.load(f)

        if collection_id in mappings:
            del mappings[collection_id]

            with open(mapping_file, 'w', encoding='utf-8') as f:
                json.dump(mappings, f, ensure_ascii=False, indent=2)

            print(f"Deleted mapping: {collection_id}")

    def create_knowledge_base(self, display_name: str, embedding_dim: int = None) -> tuple:
        """
        创建知识库（Collection）

        Args:
            display_name: 用户输入的显示名称（可以是中文）
            embedding_dim: embedding维度

        Returns:
            (collection_id, success): 返回collection ID和是否成功
        """
        try:
            # 生成符合Milvus规范的collection ID
            collection_id = self.generate_collection_id()

            if utility.has_collection(collection_id):
                # 理论上不会发生，因为使用时间戳
                collection_id = self.generate_collection_id()

            if embedding_dim is None:
                embedding_dim = self.get_model_dimension(self.embedding_model_name)

            schema = self.create_collection_schema(embedding_dim)
            collection = Collection(name=collection_id, schema=schema)

            # 创建索引
            index_params = {
                "metric_type": "COSINE",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 1024}
            }
            collection.create_index(field_name="embedding", index_params=index_params)

            # 参考milvus_kb_service: 创建后立即加载collection
            collection.load()

            # 保存名称映射
            self.save_name_mapping(collection_id, display_name)

            print(f"Created knowledge base: {collection_id} ('{display_name}') with embedding_dim: {embedding_dim}")
            return collection_id, True

        except Exception as e:
            print(f"Failed to create knowledge base: {e}")
            raise
    
    def delete_knowledge_base(self, collection_id: str) -> bool:
        """删除知识库"""
        try:
            if utility.has_collection(collection_id):
                utility.drop_collection(collection_id)
                # 同时删除名称映射
                self.delete_name_mapping(collection_id)
                print(f"Deleted knowledge base: {collection_id}")
                return True
            return False
        except Exception as e:
            print(f"Failed to delete knowledge base: {e}")
            raise
    
    def create_or_get_collection(self, collection_name: str, embedding_dim: int = None) -> Collection:
        """创建或获取Collection"""
        if utility.has_collection(collection_name):
            collection = Collection(collection_name)
            # 确保已加载
            try:
                collection.load()
            except Exception as e:
                print(f"加载已存在的collection时出错: {e}")
        else:
            if embedding_dim is None:
                embedding_dim = self.get_model_dimension(self.embedding_model_name)
                
            schema = self.create_collection_schema(embedding_dim)
            collection = Collection(name=collection_name, schema=schema)
            
            index_params = {
                "metric_type": "COSINE",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 1024}
            }
            collection.create_index(field_name="embedding", index_params=index_params)
            
            # 参考milvus_kb_service: 创建后立即加载
            collection.load()
        
        return collection
    
    def parse_json_file(self, json_data: Dict[str, Any]) -> List[DocumentChunk]:
        """解析JSON文件并转换为DocumentChunk列表"""
        try:
            documents = []
            
            # 提取基本信息
            filename = json_data.get("filename", "unknown.txt")
            if filename.startswith("/"):
                filename = os.path.basename(filename)
            
            data_section = json_data.get("data", {})
            chunks = data_section.get("chunks", [])
                        
            # 为每个chunk创建DocumentChunk
            for i, chunk in enumerate(chunks):
                chunk_text = chunk.get("text", "")
                if not chunk_text.strip():
                    print(f"跳过空chunk {i}")
                    continue
                
                # 构建metadata
                metadata = {
                    "page_start": chunk.get("page_start", 1),
                    "page_end": chunk.get("page_end", 1),
                    "pages": chunk.get("pages", [1]),
                    "text_length": chunk.get("text_length", len(chunk_text)),
                    "continued": chunk.get("continued", False),
                    "cross_page_bridge": chunk.get("cross_page_bridge", False),
                    "is_table_like": chunk.get("is_table_like", False),
                    "chunk_index": i
                }
                
                # 添加文档级别的metadata
                if "metadata" in data_section:
                    metadata.update(data_section["metadata"])
                
                doc = DocumentChunk(
                    chunk_text=chunk_text,
                    filename=filename,
                    file_id=str(uuid.uuid4()),
                    metadata=metadata
                )
                documents.append(doc)
            
            return documents
            
        except Exception as e:
            print(f"Failed to parse JSON file: {e}")
            raise HTTPException(status_code=400, detail=f"JSON解析失败: {str(e)}")
    
    def insert_documents(self, collection_name: str, documents: List[DocumentChunk]) -> List[str]:
        """
        批量插入文档到Collection
        参考milvus_kb_service.py的操作方式
        """
        if not documents:
            return []
        
        try:
            # 准备数据
            print(f"准备 {len(documents)} 个文档数据")
            
            chunk_texts = []
            filenames = []
            file_ids = []
            metadatas = []
            
            for doc in documents:
                chunk_texts.append(doc.chunk_text)
                filenames.append(doc.filename)
                file_ids.append(doc.file_id or str(uuid.uuid4()))
                metadatas.append(json.dumps(doc.metadata, ensure_ascii=False))
            
            # 批量生成embeddings
            print(f"批量生成embeddings")
            EMBED_BATCH_SIZE = 10
            embeddings = self.generate_embeddings_batch(chunk_texts, batch_size=EMBED_BATCH_SIZE)
            
            # 检测embedding维度
            embedding_dim = len(embeddings[0])
            print(f"检测到embedding维度: {embedding_dim}")
            
            # 创建或获取collection（会自动load）
            collection = self.create_or_get_collection(collection_name, embedding_dim)
            
            # 分批插入Milvus
            print(f"分批插入Milvus")
            MILVUS_BATCH_SIZE = 1000
            total_docs = len(documents)
            
            current_time = datetime.now().isoformat()
            
            for i in range(0, total_docs, MILVUS_BATCH_SIZE):
                end_idx = min(i + MILVUS_BATCH_SIZE, total_docs)
                
                # 准备当前批次数据 - 参考milvus_kb_service.py的entities格式
                entities = [
                    chunk_texts[i:end_idx],
                    filenames[i:end_idx],
                    file_ids[i:end_idx],
                    embeddings[i:end_idx],
                    metadatas[i:end_idx],
                    [current_time] * (end_idx - i)
                ]
                
                # 插入 - 参考milvus_kb_service.py，不调用flush
                collection.insert(entities)
            
            print(f"插入完成，共 {len(file_ids)} 条文档")
            
            # 插入完成后，确保collection保持加载状态（虽然理论上已经加载）
            try:
                if not collection.has_index():
                    collection.load()
                    print("插入后重新加载collection")
            except Exception as load_err:
                print(f"插入后加载collection警告: {load_err}")
            
            return file_ids
            
        except Exception as e:
            print(f"插入文档失败: {e}")
            raise HTTPException(status_code=500, detail=f"插入文档失败: {str(e)}")
    
    def search_by_text(self, collection_name: str, query_text: str, 
                      top_k: int = 10, filter_expr: Optional[str] = None) -> List[Dict]:
        """根据文本搜索相似文档"""
        try:
            if not utility.has_collection(collection_name):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_name} 不存在")
            
            # 生成查询向量
            print(f"为查询文本生成embedding: {query_text[:50]}...")
            query_embedding = self.generate_embedding(query_text)
            
            collection = Collection(collection_name)
            
            # 参考milvus_kb_service: 确保collection已加载
            try:
                # 检查collection是否有索引（间接判断是否需要load）
                if not collection.has_index():
                    print(f"Collection '{collection_name}' 没有索引，正在加载...")
                    collection.load()
                else:
                    # 即使有索引，也尝试load以确保在内存中
                    collection.load()
                print(f"Collection '{collection_name}' 已确保加载")
            except Exception as load_err:
                print(f"加载collection时出错: {load_err}")
                # 尝试继续，可能已经加载
            
            search_params = {
                "metric_type": "COSINE",
                "params": {"nprobe": 10}
            }
            
            output_fields = ["chunk_text", "filename", "file_id", "metadata", "created_at"]
            
            print(f"执行向量搜索，top_k: {top_k}")
            results = collection.search(
                data=[query_embedding],
                anns_field="embedding",
                param=search_params,
                limit=top_k,
                expr=filter_expr,
                output_fields=output_fields
            )
            
            # 格式化结果
            formatted_results = []
            for hits in results:
                for hit in hits:
                    entity = getattr(hit, "entity", None)
                    metadata_raw = entity.get("metadata") if entity else None

                    metadata = {}
                    if isinstance(metadata_raw, str) and metadata_raw:
                        try:
                            metadata = json.loads(metadata_raw)
                        except json.JSONDecodeError:
                            metadata = {}
                    elif isinstance(metadata_raw, dict):
                        metadata = metadata_raw

                    result = {
                        "id": hit.id,
                        "score": hit.score,
                        "chunk_text": entity.get("chunk_text") if entity else None,
                        "filename": entity.get("filename") if entity else None,
                        "file_id": entity.get("file_id") if entity else None,
                        "metadata": metadata,
                        "created_at": entity.get("created_at") if entity else None
                    }
                    formatted_results.append(result)
            
            formatted_results = self._apply_hybrid_scores(query_text, formatted_results)
            
            print(f"搜索完成，找到 {len(formatted_results)} 个结果")
            return formatted_results
            
        except Exception as e:
            print(f"搜索失败: {e}")
            import traceback
            print(f"详细错误: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")
    
    def search_by_filename(self, collection_name: str, filename: str, top_k: int = 10) -> List[Dict]:
        """根据文件名检索知识库内信息"""
        try:
            if not utility.has_collection(collection_name):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_name} 不存在")
            
            collection = Collection(collection_name)
            
            # 确保collection已加载
            try:
                collection.load()
                print(f"Collection '{collection_name}' 已加载")
            except Exception as load_err:
                print(f"加载collection警告: {load_err}")
            
            # 构建查询表达式
            expr = f'filename == "{filename}"'
            print(f"根据文件名查询: {expr}")
            
            # 执行查询
            results = collection.query(
                expr=expr,
                limit=top_k,
                output_fields=["chunk_text", "filename", "file_id", "metadata", "created_at"]
            )
            
            # 格式化结果
            formatted_results = []
            for result in results:
                formatted_result = {
                    "id": result.get("id"),
                    "chunk_text": result.get("chunk_text"),
                    "filename": result.get("filename"),
                    "file_id": result.get("file_id"),
                    "metadata": json.loads(result.get("metadata", "{}")),
                    "created_at": result.get("created_at")
                }
                formatted_results.append(formatted_result)
            
            print(f"根据文件名 {filename} 找到 {len(formatted_results)} 个结果")
            return formatted_results
            
        except Exception as e:
            print(f"根据文件名检索失败: {e}")
            import traceback
            print(f"详细错误: {traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"根据文件名检索失败: {str(e)}")
    
    def delete_documents_by_filename(self, collection_name: str, filename: str) -> int:
        """根据文件名删除文档"""
        try:
            if not utility.has_collection(collection_name):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_name} 不存在")
            
            collection = Collection(collection_name)
            
            # 删除前确保collection已加载
            try:
                collection.load()
            except Exception as load_err:
                print(f"加载collection警告: {load_err}")
            
            # 构建删除表达式
            expr = f'filename == "{filename}"'
            print(f"删除文件: {expr}")
            
            # 执行删除 - 参考milvus_kb_service.py，不调用flush
            collection.delete(expr)
            
            print(f"删除文件 {filename} 成功")
            return 1
            
        except Exception as e:
            print(f"删除失败: {e}")
            raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")
    

    def delete_documents_by_file_id(self, collection_id: str, file_id: str) -> int:
        """根据 file_id 删除文档（删除该文件的所有 chunks）

        Args:
            collection_id: Milvus 中的 collection 名称/ID
            file_id: 文件唯一 ID（和插入时的 file_id 字段一致）

        Returns:
            已删除的记录数（Milvus 无法精准返回删除条数时，返回 1 代表执行成功）
        """
        try:
            if not utility.has_collection(collection_id):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_id} 不存在")

            collection = Collection(collection_id)
            # 确保 collection 已加载
            try:
                collection.load()
            except Exception as load_err:
                print(f"加载collection警告: {load_err}")

            # 构建删除表达式并执行删除
            expr = f'file_id == "{file_id}"'
            print(f"根据 file_id 删除: {expr}")
            collection.delete(expr)

            # 与本服务其他删除接口保持一致，这里不显式 flush
            print(f"删除 file_id={file_id} 对应的向量成功")
            return 1

        except HTTPException:
            raise
        except Exception as e:
            print(f"根据 file_id 删除失败: {e}")
            raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")

    def get_chunks_by_file_id(self, collection_id: str, file_id: str) -> List[Dict[str, Any]]:
        """查询某个文档(file_id)在指定知识库(collection)中的所有chunks（返回Milvus主键id）"""
        try:
            if not file_id:
                raise HTTPException(status_code=400, detail="缺少 file_id")
            if not utility.has_collection(collection_id):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_id} 不存在")

            collection = Collection(collection_id)
            try:
                collection.load()
            except Exception as load_err:
                print(f"加载collection警告: {load_err}")

            expr = f'file_id == "{file_id}"'
            results = collection.query(
                expr=expr,
                output_fields=["id", "chunk_text", "filename", "file_id", "metadata", "created_at"],
                limit=16384,
            )

            formatted: List[Dict[str, Any]] = []
            for result in results:
                metadata_raw = result.get("metadata", "{}")
                try:
                    metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
                except Exception:
                    metadata = {}
                # Convert id to string to avoid JavaScript number precision loss
                # (Milvus INT64 can exceed Number.MAX_SAFE_INTEGER)
                chunk_id = result.get("id")
                formatted.append(
                    {
                        "id": str(chunk_id) if chunk_id is not None else None,
                        "chunk_text": result.get("chunk_text"),
                        "filename": result.get("filename"),
                        "file_id": result.get("file_id"),
                        "metadata": metadata,
                        "created_at": result.get("created_at"),
                    }
                )

            formatted.sort(key=lambda x: int((x.get("metadata") or {}).get("chunk_index", 0)))
            return formatted
        except HTTPException:
            raise
        except Exception as e:
            print(f"查询chunks失败: {e}")
            raise HTTPException(status_code=500, detail=f"查询chunks失败: {str(e)}")

    def copy_chunks(
        self,
        source_collection_id: str,
        target_collection_id: str,
        chunk_ids: List[str],  # Accept strings to avoid JS number precision loss
        new_file_id: Optional[str] = None,
        new_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """在Milvus层直接复制chunks（包含embedding向量），避免重复embedding计算"""
        started_at = time.time()
        if not chunk_ids:
            raise HTTPException(status_code=400, detail="chunk_ids 不能为空")
        if source_collection_id == target_collection_id:
            raise HTTPException(status_code=400, detail="source_collection_id 与 target_collection_id 不能相同")
        if not utility.has_collection(source_collection_id):
            raise HTTPException(status_code=404, detail=f"源知识库 {source_collection_id} 不存在")
        if not utility.has_collection(target_collection_id):
            raise HTTPException(status_code=404, detail=f"目标知识库 {target_collection_id} 不存在")

        if not new_file_id:
            new_file_id = f"copied_{int(time.time() * 1000)}"

        source_collection = Collection(source_collection_id)
        target_collection = Collection(target_collection_id)
        try:
            source_collection.load()
        except Exception as load_err:
            print(f"加载源collection警告: {load_err}")
        try:
            target_collection.load()
        except Exception as load_err:
            print(f"加载目标collection警告: {load_err}")

        def _get_embedding_dim(col: Collection) -> Optional[int]:
            try:
                for f in col.schema.fields:
                    if getattr(f, "name", None) == "embedding":
                        params = getattr(f, "params", None) or {}
                        dim = params.get("dim") if isinstance(params, dict) else None
                        return int(dim) if dim is not None else None
            except Exception:
                return None
            return None

        source_dim = _get_embedding_dim(source_collection)
        target_dim = _get_embedding_dim(target_collection)
        if source_dim is not None and target_dim is not None and source_dim != target_dim:
            raise HTTPException(
                status_code=400,
                detail=f"源/目标知识库向量维度不一致：source={source_dim}, target={target_dim}，无法直接复制向量",
            )

        chunk_copy_logger.info(
            "copy start source=%s target=%s new_file_id=%s new_filename=%s requested=%d",
            source_collection_id,
            target_collection_id,
            new_file_id,
            new_filename,
            len(chunk_ids),
        )

        copied_count = 0
        found_count = 0
        skipped_no_embedding_count = 0
        used_get_fallback = False
        current_time = datetime.now().isoformat()

        QUERY_BATCH_SIZE = 512
        INSERT_BATCH_SIZE = 512

        for start in range(0, len(chunk_ids), QUERY_BATCH_SIZE):
            batch_ids_str = chunk_ids[start:start + QUERY_BATCH_SIZE]
            # Convert string IDs to integers for Milvus query
            batch_ids = [int(id_str) for id_str in batch_ids_str]
            expr = f"id in {batch_ids}"
            output_fields = ["id", "chunk_text", "filename", "file_id", "embedding", "metadata", "created_at"]

            results: List[Dict[str, Any]] = []
            try:
                results = source_collection.query(
                    expr=expr,
                    output_fields=output_fields,
                    limit=len(batch_ids),
                )
            except Exception as e:
                chunk_copy_logger.info("copy query failed via query() batch_size=%d err=%s", len(batch_ids), e)
                results = []

            # Some pymilvus/milvus versions may not return vector fields in query results.
            if results:
                missing_embeddings = sum(1 for r in results if r.get("embedding") is None)
                if missing_embeddings == len(results) and hasattr(source_collection, "get"):
                    try:
                        results = source_collection.get(batch_ids, output_fields=output_fields)  # type: ignore[attr-defined]
                        used_get_fallback = True
                    except Exception as e:
                        chunk_copy_logger.info("copy query fallback via get() failed batch_size=%d err=%s", len(batch_ids), e)

            if not results:
                continue
            found_count += len(results)

            source_filenames = {r.get("filename") for r in results if r.get("filename")}
            if not new_filename:
                if len(source_filenames) == 1:
                    only_name = next(iter(source_filenames))
                    new_filename = f"【复制】{only_name}"
                else:
                    new_filename = "【复制】多个来源文档"

            # Prepare insert entities (no primary id)
            chunk_texts: List[str] = []
            filenames: List[str] = []
            file_ids: List[str] = []
            embeddings: List[List[float]] = []
            metadatas: List[str] = []
            created_ats: List[str] = []

            for r in results:
                embedding = r.get("embedding")
                if embedding is None:
                    skipped_no_embedding_count += 1
                    continue

                metadata_raw = r.get("metadata", "{}")
                try:
                    metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
                except Exception:
                    metadata = {}

                source_file_id = r.get("file_id")
                metadata["source_file_id"] = source_file_id
                metadata["source_collection_id"] = source_collection_id
                metadata["source_chunk_id"] = r.get("id")
                metadata["copied_at"] = current_time
                metadata["file_id"] = new_file_id

                chunk_texts.append(r.get("chunk_text", ""))
                filenames.append(new_filename)
                file_ids.append(new_file_id)
                embeddings.append(embedding)
                metadatas.append(json.dumps(metadata, ensure_ascii=False))
                created_ats.append(current_time)

            for i in range(0, len(chunk_texts), INSERT_BATCH_SIZE):
                end = min(i + INSERT_BATCH_SIZE, len(chunk_texts))
                entities = [
                    chunk_texts[i:end],
                    filenames[i:end],
                    file_ids[i:end],
                    embeddings[i:end],
                    metadatas[i:end],
                    created_ats[i:end],
                ]
                target_collection.insert(entities)
                copied_count += (end - i)

        elapsed_ms = int((time.time() - started_at) * 1000)
        chunk_copy_logger.info(
            "copy done source=%s target=%s requested=%d found=%d copied=%d skipped_no_embedding=%d used_get=%s elapsed_ms=%d",
            source_collection_id,
            target_collection_id,
            len(chunk_ids),
            found_count,
            copied_count,
            skipped_no_embedding_count,
            used_get_fallback,
            elapsed_ms,
        )
        return {
            "requested_count": len(chunk_ids),
            "found_count": found_count,
            "copied_count": copied_count,
            "skipped_no_embedding_count": skipped_no_embedding_count,
            "used_get_fallback": used_get_fallback,
            "new_file_id": new_file_id,
            "new_filename": new_filename,
        }

    def list_knowledge_bases(self) -> List[Dict[str, Any]]:
        """列出所有知识库，包含display_name"""
        try:
            collection_ids = utility.list_collections()
            mappings = self.get_name_mapping()  # 获取所有映射

            result = []
            for collection_id in collection_ids:
                mapping = mappings.get(collection_id, {})
                result.append({
                    "collection_id": collection_id,
                    "display_name": mapping.get("display_name", collection_id),
                    "created_at": mapping.get("created_at", "")
                })

            return result
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"获取知识库列表失败: {str(e)}")

    def get_collection_stats(self, collection_id: str) -> Dict[str, Any]:
        """获取知识库统计信息"""
        try:
            if not utility.has_collection(collection_id):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_id} 不存在")

            collection = Collection(collection_id)

            # 确保collection已加载
            try:
                collection.load()
            except Exception as load_err:
                print(f"加载collection警告: {load_err}")

            # 获取实体总数（chunk数）
            total_chunks = collection.num_entities

            # 查询所有文档，获取唯一文件名数量
            try:
                # 查询所有记录的filename字段
                results = collection.query(
                    expr="id > 0",
                    output_fields=["filename", "created_at"],
                    limit=16384  # Milvus默认最大限制
                )

                # 统计唯一文件名
                unique_filenames = set()
                latest_update = None

                for result in results:
                    filename = result.get("filename")
                    if filename:
                        unique_filenames.add(filename)

                    # 获取最新更新时间
                    created_at = result.get("created_at")
                    if created_at:
                        if latest_update is None or created_at > latest_update:
                            latest_update = created_at

                total_documents = len(unique_filenames)

            except Exception as query_err:
                print(f"查询统计信息失败: {query_err}")
                total_documents = 0
                latest_update = None

            # 获取显示名称
            mapping = self.get_name_mapping(collection_id)
            display_name = mapping.get("display_name", collection_id)

            return {
                "collection_id": collection_id,
                "collection_name": display_name,  # 前端显示的名称
                "total_documents": total_documents,
                "total_chunks": total_chunks,
                "last_updated": latest_update
            }

        except Exception as e:
            print(f"获取统计信息失败: {e}")
            raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")

    def get_collection_documents(self, collection_id: str) -> List[Dict[str, Any]]:
        """获取知识库中的文档列表（按文件名去重，支持 V1 和 V2）"""
        try:
            if not utility.has_collection(collection_id):
                raise HTTPException(status_code=404, detail=f"知识库 {collection_id} 不存在")

            collection = Collection(collection_id)

            # 确保collection已加载
            try:
                collection.load()
            except Exception as load_err:
                print(f"加载collection警告: {load_err}")

            # 查询所有记录
            try:
                results = collection.query(
                    expr="id > 0",
                    output_fields=["filename", "file_id", "metadata", "created_at"],
                    limit=16384
                )

                print(f"\n📊 查询知识库文档列表: {collection_id}")
                print(f"  - 总记录数: {len(results)}")

                # 按文件名分组统计
                doc_stats = {}
                for idx, result in enumerate(results):
                    filename = result.get("filename")
                    if not filename:
                        continue

                    if filename not in doc_stats:
                        # 解析metadata获取额外信息
                        metadata_str = result.get("metadata", "{}")
                        try:
                            metadata = json.loads(metadata_str)
                        except:
                            metadata = {}

                        # ✅ 关键：从 metadata 中提取文件系统的 file_id
                        # V1: metadata.file_id (如 file_20251016_d398cfa1)
                        # V2: metadata.file_id (UUID 格式，如 bdde5a1e-b522-4de5-9948-2e1d721a7292)
                        # 如果 metadata 中没有，则使用 Milvus 的 file_id
                        actual_file_id = metadata.get("file_id", result.get("file_id"))
                        
                        # 打印前3个文档的 ID 信息
                        if idx < 3:
                            print(f"\n  文档 {idx+1}: {filename}")
                            print(f"    - Milvus file_id: {result.get('file_id')}")
                            print(f"    - Metadata file_id: {metadata.get('file_id')}")
                            print(f"    - 实际使用 file_id: {actual_file_id}")
                            print(f"    - API version: {metadata.get('api_version', 'v1')}")

                        doc_stats[filename] = {
                            "filename": filename,
                            "file_id": actual_file_id,  # ✅ 使用 metadata 中的 file_id
                            "chunks": 0,
                            "created_at": result.get("created_at"),
                            "metadata": metadata,
                            "api_version": metadata.get("api_version", "v1")  # ✅ 添加版本信息
                        }

                    doc_stats[filename]["chunks"] += 1

                # 转换为列表并排序（按创建时间倒序）
                documents = list(doc_stats.values())
                documents.sort(key=lambda x: x.get("created_at", ""), reverse=True)

                print(f"\n  ✓ 去重后文档数: {len(documents)}\n")

                return documents

            except Exception as query_err:
                print(f"查询文档列表失败: {query_err}")
                return []

        except Exception as e:
            print(f"获取文档列表失败: {e}")
            raise HTTPException(status_code=500, detail=f"获取文档列表失败: {str(e)}")

    def get_all_stats(self) -> Dict[str, Any]:
        """获取所有知识库的汇总统计信息"""
        try:
            collections = utility.list_collections()

            total_collections = len(collections)
            total_documents = 0
            total_chunks = 0

            collection_details = []

            for collection_name in collections:
                try:
                    stats = self.get_collection_stats(collection_name)
                    total_documents += stats["total_documents"]
                    total_chunks += stats["total_chunks"]
                    collection_details.append(stats)
                except Exception as e:
                    print(f"获取 {collection_name} 统计信息失败: {e}")
                    continue

            return {
                "total_collections": total_collections,
                "total_documents": total_documents,
                "total_chunks": total_chunks,
                "collections": collection_details
            }

        except Exception as e:
            print(f"获取汇总统计失败: {e}")
            raise HTTPException(status_code=500, detail=f"获取汇总统计失败: {str(e)}")

# FastAPI 应用
app = FastAPI(
    title="Milvus RAG Service",
    version="2.0.0",
    dependencies=[Depends(verify_jwt)],
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源，生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有HTTP方法
    allow_headers=["*"],  # 允许所有headers
)

# 初始化Milvus服务
milvus_service = MilvusRAGService()

@app.post("/knowledge_base/create", dependencies=[Depends(require_admin)])
async def create_knowledge_base(display_name: str):
    """创建知识库"""
    try:
        collection_id, success = milvus_service.create_knowledge_base(display_name)

        if success:
            return {
                "status": "success",
                "message": f"知识库 '{display_name}' 创建成功",
                "collection_id": collection_id,
                "display_name": display_name
            }
        else:
            return {
                "status": "error",
                "message": f"知识库创建失败"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class DeleteKBByIdRequest(BaseModel):
    collection_id: str

@app.delete("/knowledge_base/delete", dependencies=[Depends(require_admin)])
async def delete_knowledge_base(request: DeleteKBByIdRequest):
    """删除知识库"""
    try:
        success = milvus_service.delete_knowledge_base(request.collection_id)
        if success:
            return {"status": "success", "message": f"知识库删除成功"}
        else:
            return {"status": "not_found", "message": f"知识库不存在"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/knowledge_base/list")
async def list_knowledge_bases():
    """列出所有知识库"""
    try:
        kbs = milvus_service.list_knowledge_bases()
        return {"status": "success", "knowledge_bases": kbs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== v2 Markdown Edit + Re-split/Re-embed ====================

class MarkdownUpdateRequest(BaseModel):
    content: str

class ResplitReembedRequest(BaseModel):
    collection_id: str
    method: Optional[str] = None  # default ocr_aware
    chunk_size: Optional[int] = None  # default 1500
    chunk_overlap: Optional[int] = None  # default 200
    max_page_span: Optional[int] = None  # default 3


def _resolve_v2_context_or_400(file_id: str) -> Dict[str, Any]:
    """Locate v2 files and metadata. Raise 400 for non-v2 docs.

    Returns dict with: extraction_dir, metadata(dict), filename(str), markdown_path(Path),
    result_key(str), chunked_output_path(Path)
    """
    extraction_dir = EXTRACTION_RESULTS_DIR / file_id
    if not extraction_dir.exists():
        raise HTTPException(status_code=404, detail=f"文档 {file_id} 不存在")

    # Detect v2 by presence of *_extraction.md or *_chunked_output.json
    md_candidates = sorted(extraction_dir.glob("*_extraction.md"))
    chunked_candidates = sorted(extraction_dir.glob("*_chunked_output.json"))
    if not md_candidates and not chunked_candidates:
        # not v2 (v1 uses chunks.json and <filename>.md)
        raise HTTPException(status_code=400, detail="仅支持 v2 文档的编辑和重切分，此文档不是 v2")

    # Load metadata (prefer *_metadata.json)
    metadata_path = None
    for p in sorted(extraction_dir.glob("*_metadata.json")):
        metadata_path = p
        break
    if metadata_path is None:
        # fallback to generic metadata.json if it exists
        p = extraction_dir / "metadata.json"
        if p.exists():
            metadata_path = p
    if metadata_path is None:
        raise HTTPException(status_code=404, detail="未找到 v2 元数据文件")

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        raise HTTPException(status_code=500, detail="读取元数据失败")

    filename = meta.get("filename") or meta.get("metadata", {}).get("filename")
    if not filename:
        raise HTTPException(status_code=500, detail="元数据缺少文件名")

    # Determine markdown path
    if md_candidates:
        md_path = md_candidates[0]
    else:
        # If no md exists (rare), create a placeholder path using extraction mode prefix
        # e.g., 'mineru_extraction.md'
        # Try to infer prefix from metadata filename (deepseek/mineru/paddleocr_vl)
        prefix = "mineru"  # default
        for cand in ("mineru", "deepseek", "paddleocr_vl"):
            for p in extraction_dir.glob(f"{cand}_metadata.json"):
                prefix = cand
                break
        md_path = extraction_dir / f"{prefix}_extraction.md"

    # Determine result_key for chunker: base filename without extension
    result_key = Path(filename).stem

    # Decide chunked output path
    if chunked_candidates:
        chunked_path = chunked_candidates[0]
    else:
        # Use <prefix>_chunked_output.json if a prefix is implied by md file
        stem = md_path.name.replace("_extraction.md", "")
        chunked_path = extraction_dir / f"{stem}_chunked_output.json"

    return {
        "extraction_dir": extraction_dir,
        "metadata": meta,
        "filename": filename,
        "markdown_path": md_path,
        "result_key": result_key,
        "chunked_output_path": chunked_path,
    }


@app.post("/document/{file_id}/markdown", dependencies=[Depends(require_admin)])
async def update_markdown_v2(file_id: str, request: MarkdownUpdateRequest):
    """Overwrite v2 markdown file with user-edited content. v1 is not supported."""
    ctx = _resolve_v2_context_or_400(file_id)
    md_path: Path = ctx["markdown_path"]

    # Write new content (no backup per requirement)
    try:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(request.content or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存Markdown失败: {e}")

    return {
        "status": "success",
        "message": "Markdown 已更新",
        "path": str(md_path),
        "bytes": len((request.content or "").encode("utf-8"))
    }


@app.post("/document/{file_id}/resplit_reembed", dependencies=[Depends(require_admin)])
async def resplit_and_reembed_v2(file_id: str, body: ResplitReembedRequest):
    """For v2 docs only: re-split edited Markdown with v2 splitter, then re-embed and replace vectors in Milvus."""
    if not body.collection_id:
        raise HTTPException(status_code=400, detail="缺少 collection_id")

    ctx = _resolve_v2_context_or_400(file_id)
    md_path: Path = ctx["markdown_path"]
    result_key: str = ctx["result_key"]
    chunked_output_path: Path = ctx["chunked_output_path"]
    filename: str = ctx["filename"]

    # Read markdown
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            markdown = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取Markdown失败: {e}")

    # Prepare output_json wrapper for v2 chunker
    output_json = {
        "backend": "pipeline",
        "version": "2.0",
        "results": {
            result_key: {
                "md_content": markdown
            }
        }
    }

    config = {
        "method": body.method or "ocr_aware",
        "chunk_size": body.chunk_size or 1500,
        "chunk_overlap": body.chunk_overlap or 200,
        "merge_tolerance": 0.2,
        "max_page_span": body.max_page_span or 3,
        "bridge_span": 150,
        "add_bridges": True,
    }

    # Call v2 chunking service
    # Prefer httpx if available; else fallback to requests
    try:
        try:
            import httpx as _httpx
            with _httpx.Client(timeout=None) as client:
                resp = client.post(
                    CHUNKING_SERVICE_URL_V2,
                    json={
                        "output_json": output_json,
                        "result_key": result_key,
                        "config": config,
                        "file_id": file_id,
                    },
                )
                resp.raise_for_status()
                chunked_json = resp.json()
        except Exception:
            # Fallback to requests
            import requests as _requests
            resp = _requests.post(
                CHUNKING_SERVICE_URL_V2,
                json={
                    "output_json": output_json,
                    "result_key": result_key,
                    "config": config,
                    "file_id": file_id,
                },
                timeout=120,
            )
            if resp.status_code >= 400:
                raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
            chunked_json = resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切分服务调用失败: {e}")

    # Persist chunked output (full object, as viewer expects)
    try:
        chunked_output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(chunked_output_path, "w", encoding="utf-8") as f:
            json.dump(chunked_json, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存切分结果失败: {e}")

    # Extract chunks from response
    try:
        chunks = chunked_json.get("results", {}).get(result_key, {}).get("chunks", [])
    except Exception:
        chunks = []

    # Replace Milvus vectors for this file_id
    try:
        # 1) delete old
        _ = milvus_service.delete_documents_by_file_id(body.collection_id, file_id)

        # 2) build documents list
        documents: List[DocumentChunk] = []
        for i, ch in enumerate(chunks):
            text = ch.get("text", "")
            if not text.strip():
                continue
            md = {
                "file_id": file_id,
                "page_start": ch.get("page_start", 1),
                "page_end": ch.get("page_end", 1),
                "pages": ch.get("pages", [1]),
                "text_length": ch.get("text_length", len(text)),
                "continued": ch.get("continued", False),
                "cross_page_bridge": ch.get("cross_page_bridge", False),
                "is_table_like": ch.get("is_table_like", False),
                "chunk_index": i,
                "headers": ch.get("headers", {}),
                "api_version": "v2",
            }
            # images (if any) from v2 chunkers
            if isinstance(ch.get("images"), list) and ch.get("images"):
                md["images"] = ch["images"]

            documents.append(
                DocumentChunk(
                    chunk_text=text,
                    filename=filename,
                    file_id=file_id,
                    metadata=md,
                )
            )

        # 3) insert new
        _inserted_ids = milvus_service.insert_documents(body.collection_id, documents)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Milvus 重建向量失败: {e}")

    return {
        "status": "success",
        "message": "v2 重切分并重建向量完成",
        "file_id": file_id,
        "collection_id": body.collection_id,
        "total_chunks": len(chunks),
        "chunked_output_path": str(chunked_output_path),
    }

@app.post("/upload_json", response_model=UploadResponse, dependencies=[Depends(require_admin)])
async def upload_json_file(request: UploadKBRequest):
    """上传JSON文件到知识库（V1 格式）"""
    try:
        # 解析JSON文件
        documents = milvus_service.parse_json_file(request.file_data)
        
        if not documents:
            raise HTTPException(status_code=400, detail="未找到有效的文档chunks")
        
        # 插入文档
        file_ids = milvus_service.insert_documents(request.collection_name, documents)
        
        # 获取文件名
        filename = request.file_data.get("filename", "unknown.txt")
        if filename.startswith("/"):
            filename = os.path.basename(filename)
        
        response = UploadResponse(
            filename=filename,
            file_id=documents[0].file_id if documents else str(uuid.uuid4()),
            status="success",
            message=f"文件上传成功（V1），处理了{len(documents)}个chunks",
            chunks_count=len(documents)
        )
        
        return response
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload_json/v2", dependencies=[Depends(require_admin)])
async def upload_json_file_v2(request: Dict[str, Any]):
    """
    上传 output.json 格式的数据到知识库（OCR 2.0）
    
    请求格式：
    {
      "collection_name": "kb_123",
      "file_id": "原始file_id",  // 必须，用于前端查找文件
      "output_json": {
        "backend": "pipeline",
        "version": "2.5.4",
        "results": {
          "文件名": {
            "md_content": "...",
            "chunks": [...],
            "images": {...},
            "page_images": [...]
          }
        }
      },
      "result_key": "文件名"  // 可选
    }
    
    返回：存储结果
    """
    try:
        
        # 提取请求参数
        collection_name = request.get("collection_name")
        file_id_original = request.get("file_id")  # 原始 file_id
        output_json = request.get("output_json", {})
        result_key = request.get("result_key")
        
        if not collection_name:
            raise HTTPException(status_code=400, detail="缺少 collection_name 参数")
        
        if not file_id_original:
            raise HTTPException(status_code=400, detail="缺少 file_id 参数")
        
        print(f"📋 请求参数:")
        print(f"  - Collection: {collection_name}")
        print(f"  - File ID: {file_id_original}")
        print(f"  - Result Key: {result_key or '(自动)'}")
        
        # 提取 results
        results = output_json.get("results", {})
        if not results:
            print(f"❌ output_json 结构:")
            print(f"  - Keys: {list(output_json.keys())}")
            raise HTTPException(status_code=400, detail="output_json 中未找到 results 字段")
        
        # 获取 result_key
        if not result_key or result_key not in results:
            result_key = list(results.keys())[0]
            print(f"ℹ️  使用第一个键作为 result_key: {result_key}")
        
        result_data = results[result_key]
        
        print(f"\n📦 输入数据信息:")
        print(f"  - Backend: {output_json.get('backend', 'N/A')}")
        print(f"  - Version: {output_json.get('version', 'N/A')}")
        print(f"  - Result Key: {result_key}")
        print(f"  - 包含字段: {list(result_data.keys())}")
        print(f"  - 是否有 md_content: {'md_content' in result_data}")
        print(f"  - 是否有 chunks: {'chunks' in result_data}")
        print(f"  - 是否有 images: {'images' in result_data}")
        print(f"  - 是否有 page_images: {'page_images' in result_data}")
        
        if 'md_content' in result_data:
            print(f"  - md_content 长度: {len(result_data['md_content'])} 字符")
        
        # 提取 chunks
        chunks = result_data.get("chunks", [])
        if not chunks:
            print(f"❌ 未找到 chunks 或 chunks 为空")
            raise HTTPException(status_code=400, detail="未找到 chunks 字段或 chunks 为空")
        
        print(f"  - 总 chunks: {len(chunks)}")
        
        # 打印前3个 chunks 的结构
        print(f"\n📝 前 3 个 chunks 结构:")
        for i, chunk in enumerate(chunks[:3]):
            print(f"  Chunk {i+1}:")
            print(f"    - Keys: {list(chunk.keys())}")
            print(f"    - 页码: {chunk.get('page_start')}-{chunk.get('page_end')}")
            print(f"    - 文本长度: {chunk.get('text_length')}")
            print(f"    - 文本预览: {chunk.get('text', '')[:50]}...")
        
        # 构建 DocumentChunk 列表
        print(f"\n🔄 开始构建 DocumentChunk 列表...")
        documents = []
        filename = result_key + ".pdf"  # 恢复文件名
        
        for i, chunk in enumerate(chunks):
            chunk_text = chunk.get("text", "")
            if not chunk_text.strip():
                print(f"  ⚠️  跳过空 chunk {i}")
                continue
            
            # 构建 metadata（包含 V2 的额外字段 + 原始 file_id）
            metadata = {
                "file_id": file_id_original,  # 保存原始 file_id
                "page_start": chunk.get("page_start", 1),
                "page_end": chunk.get("page_end", 1),
                "pages": chunk.get("pages", [1]),
                "text_length": chunk.get("text_length", len(chunk_text)),
                "continued": chunk.get("continued", False),
                "cross_page_bridge": chunk.get("cross_page_bridge", False),
                "is_table_like": chunk.get("is_table_like", False),
                "chunk_index": i,
                "headers": chunk.get("headers", {}),  # V2 特有
                "extraction_method": output_json.get("backend", "unknown"),  # V2 特有
                "version": output_json.get("version", "unknown"),  # V2 特有
                "api_version": "v2"  # 标记为 V2 数据
            }
            # 如果切分结果包含图片引用，保留到metadata，供对话模型直接引用
            if "images" in chunk and isinstance(chunk["images"], list) and chunk["images"]:
                metadata["images"] = chunk["images"]
            
            doc = DocumentChunk(
                chunk_text=chunk_text,
                filename=filename,
                file_id=file_id_original,  # ✅ 使用原始 file_id
                metadata=metadata
            )
            documents.append(doc)
        
        print(f"  ✓ 构建完成，有效 chunks: {len(documents)}")
        
        # 插入到 Milvus
        print(f"\n🗄️  开始插入到 Milvus（Collection: {collection_name}）...")
        file_ids = milvus_service.insert_documents(collection_name, documents)
        
        print("\n" + "="*80)
        print("✅ OCR 2.0 数据存储完成")
        print("="*80)
        print(f"📊 存储统计:")
        print(f"  - Collection: {collection_name}")
        print(f"  - 文件名: {filename}")
        print(f"  - 插入数量: {len(file_ids)}")
        print(f"  - Backend: {output_json.get('backend')}")
        print(f"  - Version: {output_json.get('version')}")
        print("="*80 + "\n")
        
        return {
            "status": "success",
            "message": f"OCR 2.0 数据上传成功，处理了 {len(documents)} 个 chunks",
            "collection_name": collection_name,
            "filename": filename,
            "chunks_count": len(documents),
            "backend": output_json.get("backend"),
            "version": output_json.get("version")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"\n" + "="*80)
        print(f"❌ OCR 2.0 数据存储失败")
        print("="*80)
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        print("="*80 + "\n")
        raise HTTPException(status_code=500, detail=f"OCR 2.0 数据存储失败: {str(e)}")

@app.post("/search")
async def search_documents(request: SearchRequest):
    """根据问题搜索相似文档"""
    try:
        results = milvus_service.search_by_text(
            collection_name=request.collection_name,
            query_text=request.query_text,
            top_k=request.top_k,
            filter_expr=request.filter_expr
        )
        
        return {
            "status": "success",
            "query": request.query_text,
            "results": results,
            "total": len(results)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search_by_filename")
async def search_by_filename(request: SearchByFilenameRequest):
    """根据文件名检索知识库内信息"""
    try:
        results = milvus_service.search_by_filename(
            collection_name=request.collection_name,
            filename=request.filename,
            top_k=request.top_k
        )
        
        return {
            "status": "success",
            "filename": request.filename,
            "results": results,
            "total": len(results)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete", dependencies=[Depends(require_admin)])
async def delete_documents(request: DeleteRequest):
    """删除文档"""
    try:
        deleted_count = milvus_service.delete_documents_by_filename(
            collection_name=request.collection_name,
            filename=request.filename
        )
        
        return {
            "status": "success",
            "message": f"已删除文件 {request.filename}",
            "deleted_count": deleted_count
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# New: 删除单个文档（按 file_id）
@app.delete("/document/{file_id}", dependencies=[Depends(require_admin)])
async def delete_document_by_id(file_id: str, collection_id: str):

    """删除指定知识库中的单个文档（通过 file_id 匹配并删除其所有 chunks）

    同时清理本地存储：
    - 删除提取结果目录: EXTRACTION_RESULTS_DIR/<file_id>
    - 删除上传的源PDF: UPLOAD_BASE_DIR/<collection_id>/*<file_id>*.pdf

    Query Params:
        collection_id: 知识库 ID（Milvus collection 名称）
    Path Params:
        file_id: 文件唯一 ID
    """
    try:
        # 1) 删除 Milvus 中的向量（按 file_id）
        deleted_count = milvus_service.delete_documents_by_file_id(collection_id, file_id)

        # 2) 清理本地提取产物与源PDF
        # 读取与 get_document_details 一致的环境变量与默认路径
        extraction_base = Path(os.getenv(
            "EXTRACTION_RESULTS_DIR",
            "./output/extraction_results"
        ))
        upload_base = Path(os.getenv(
            "UPLOAD_BASE_DIR",
            "./output/uploads"
        ))

        fs_deleted = {
            "extraction_dir_removed": False,
            "extraction_dir_path": str(extraction_base / file_id),
            "source_pdfs_removed": 0,
            "upload_dir_path": str(upload_base / collection_id)
        }

        # 2.a) 删除提取结果目录
        try:
            extraction_dir = extraction_base / file_id
            if extraction_dir.exists():
                shutil.rmtree(extraction_dir)
                fs_deleted["extraction_dir_removed"] = True
        except Exception as rm_err:
            # 记录但不影响整体删除成功
            print(f"清理提取目录失败: {rm_err}")

        # 2.b) 删除上传的源PDF（匹配文件名中包含 file_id 的 .pdf）
        try:
            upload_dir = upload_base / collection_id
            if upload_dir.exists():
                for p in upload_dir.iterdir():
                    # 仅删除包含 file_id 且以 .pdf 结尾的文件
                    if p.is_file() and p.suffix.lower() == ".pdf" and file_id in p.name:
                        try:
                            p.unlink()
                            fs_deleted["source_pdfs_removed"] += 1
                        except Exception as del_err:
                            print(f"删除源PDF失败 {p}: {del_err}")
        except Exception as scan_err:
            print(f"扫描上传目录失败: {scan_err}")

        return {
            "status": "success",
            "message": f"已删除文档 file_id={file_id}",
            "deleted_count": deleted_count,
            "fs_cleanup": fs_deleted,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "Milvus RAG Service"}

@app.get("/stats/collection/{collection_name}")
async def get_collection_stats(collection_name: str):
    """获取单个知识库的统计信息"""
    try:
        stats = milvus_service.get_collection_stats(collection_name)
        return {"status": "success", "data": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats/all")
async def get_all_stats():
    """获取所有知识库的汇总统计信息"""
    try:
        stats = milvus_service.get_all_stats()
        return {"status": "success", "data": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/knowledge_base/{collection_id}/documents")
async def get_knowledge_base_documents(collection_id: str):
    """获取知识库中的文档列表"""
    try:
        documents = milvus_service.get_collection_documents(collection_id)
        stats = milvus_service.get_collection_stats(collection_id)

        return {
            "status": "success",
            "collection_id": collection_id,
            "collection_name": stats["collection_name"],
            "total_documents": stats["total_documents"],
            "total_chunks": stats["total_chunks"],
            "last_updated": stats["last_updated"],
            "documents": documents
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/knowledge_base/{collection_id}/chunks")
async def get_knowledge_base_chunks(collection_id: str, file_id: str):
    """获取指定知识库中某个文档(file_id)的chunks列表（包含Milvus主键ID）"""
    try:
        chunks = milvus_service.get_chunks_by_file_id(collection_id, file_id)
        return {
            "status": "success",
            "collection_id": collection_id,
            "file_id": file_id,
            "total": len(chunks),
            "chunks": chunks,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chunks/copy", dependencies=[Depends(require_admin)])
async def copy_chunks(request: CopyChunksRequest):
    """跨知识库复制chunks（直接复制embedding向量，不重新生成）"""
    try:
        result = milvus_service.copy_chunks(
            source_collection_id=request.source_collection_id,
            target_collection_id=request.target_collection_id,
            chunk_ids=request.chunk_ids,
            new_file_id=request.new_file_id,
            new_filename=request.new_filename,
        )
        if result.get("copied_count", 0) == 0:
            chunk_copy_logger.info(
                "copy warning copied=0 source=%s target=%s requested=%d found=%d skipped_no_embedding=%d",
                request.source_collection_id,
                request.target_collection_id,
                len(request.chunk_ids or []),
                int(result.get("found_count") or 0),
                int(result.get("skipped_no_embedding_count") or 0),
            )
        return {
            "status": "success",
            **result,
            "message": f"成功复制 {result.get('copied_count', 0)} 个切片到目标知识库",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/document/{file_id}/details")
async def get_document_details(file_id: str, collection_id: Optional[str] = None):
    """获取文档的详细信息，包括PDF、Markdown、chunks等（支持 V1 和 V2）"""
    try:
        print("\n" + "="*80)
        print(f"📄 获取文档详情请求 - /document/{file_id}/details")
        print("="*80)
        print(f"  - File ID: {file_id}")
        
        # 定义基础路径（V1 和 V2 现在都使用同一个路径，从环境变量读取）
        import os
        upload_base = Path(os.getenv(
            "UPLOAD_BASE_DIR",
            "./output/uploads"
        ))
        extraction_base = Path(os.getenv(
            "EXTRACTION_RESULTS_DIR",
            "./output/extraction_results"
        ))
        
        base_dirs_v1 = {
            'upload': upload_base,
            'extraction': extraction_base
        }
        base_dirs_v2 = {
            'upload': upload_base,
            'extraction': extraction_base
        }
        
        # 查找文件目录
        print(f"\n🔍 查找文件...")
        
        extraction_dir = extraction_base / file_id
        base_upload_dir = upload_base
        
        if not extraction_dir.exists():
            print(f"  ⚠️  本地目录不存在: {extraction_dir}")
            if not collection_id:
                raise HTTPException(status_code=404, detail=f"文档 {file_id} 不存在")

            # Fallback: 复制出来的“虚拟文档”可能只有Milvus向量，没有本地提取产物
            print(f"  🔁 尝试从Milvus回退加载 chunks (collection_id={collection_id})")
            milvus_chunks = milvus_service.get_chunks_by_file_id(collection_id, file_id)
            if not milvus_chunks:
                raise HTTPException(status_code=404, detail=f"文档 {file_id} 不存在")

            first = milvus_chunks[0]
            first_meta = first.get("metadata") or {}
            api_version = str(first_meta.get("api_version", "v2"))
            filename = first.get("filename") or f"【复制】{file_id}"

            chunks_data = []
            max_page_end = 0
            for ch in milvus_chunks:
                md = ch.get("metadata") or {}
                page_start = int(md.get("page_start", 1)) if md.get("page_start") is not None else 1
                page_end = int(md.get("page_end", page_start)) if md.get("page_end") is not None else page_start
                max_page_end = max(max_page_end, page_end)
                chunks_data.append(
                    {
                        "text": ch.get("chunk_text", ""),
                        "page_start": page_start,
                        "page_end": page_end,
                        "pages": md.get("pages") or [],
                        "text_length": int(md.get("text_length", len(ch.get("chunk_text", "")))),
                        "continued": bool(md.get("continued", False)),
                        "cross_page_bridge": bool(md.get("cross_page_bridge", False)),
                        "is_table_like": bool(md.get("is_table_like", False)),
                    }
                )

            extraction_time = first.get("created_at")
            return {
                "status": "success",
                "file_id": file_id,
                "filename": filename,
                "api_version": api_version,
                "metadata": {},
                "extraction_time": extraction_time,
                "extraction_mode": None,
                "markdown": "",
                "chunks": chunks_data,
                "pdf_url": None,
                "total_chunks": len(chunks_data),
                "total_pages": max_page_end,
                "total_images": 0,
                "is_virtual_document": True,
            }
        
        print(f"  ✓ 找到目录: {extraction_dir}")
        
        # 通过文件特征判断版本（V1 vs V2）
        # V2特征：有 *_extraction.md 或 *_chunked_output.json 文件
        # V1特征：有 chunks.json 和 *.md (不含_extraction后缀)
        has_v2_extraction = len(list(extraction_dir.glob("*_extraction.md"))) > 0
        has_v2_chunked = len(list(extraction_dir.glob("*_chunked_output.json"))) > 0
        has_v1_chunks = (extraction_dir / "chunks.json").exists()
        
        if has_v2_extraction or has_v2_chunked:
            api_version = "v2"
            print(f"  ✓ 识别为 V2 格式 (有 *_extraction.md 或 *_chunked_output.json)")
        elif has_v1_chunks:
            api_version = "v1"
            print(f"  ✓ 识别为 V1 格式 (有 chunks.json)")
        else:
            # 默认为V1（兼容老数据）
            api_version = "v1"
            print(f"  ⚠️  无法明确识别版本，默认使用 V1")
        
        print(f"  - 使用版本: {api_version}")
        
        # 读取metadata.json（V1 和 V2 通用）或者 V2 的 metadata 文件
        metadata_files = list(extraction_dir.glob("*_metadata.json")) + [extraction_dir / "metadata.json"]
        metadata_path = None
        for mf in metadata_files:
            if mf.exists():
                metadata_path = mf
                break
        
        if not metadata_path:
            print(f"  ❌ 未找到 metadata 文件")
            raise HTTPException(status_code=404, detail="元数据文件不存在")

        print(f"  ✓ 找到 metadata: {metadata_path.name}")
        
        with open(metadata_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)

        # 读取chunks（V1 和 V2 格式不同）
        print(f"\n📦 读取 chunks...")
        chunks_data = []
        
        if api_version == "v2":
            # V2: 从 chunked_output.json 中读取
            chunked_files = list(extraction_dir.glob("*_chunked_output.json"))
            if chunked_files:
                chunked_file = chunked_files[0]
                print(f"  - V2 格式: {chunked_file.name}")
                with open(chunked_file, 'r') as f:
                    chunked_json = json.load(f)
                    # 从 results 中提取 chunks
                    results = chunked_json.get('results', {})
                    if results:
                        result_key = list(results.keys())[0]
                        chunks_data = results[result_key].get('chunks', [])
                print(f"  ✓ 读取到 {len(chunks_data)} 个 chunks (V2)")
            else:
                print(f"  ⚠️  未找到 V2 chunked_output.json")
        else:
            # V1: 从 chunks.json 中读取
            chunks_path = extraction_dir / "chunks.json"
            if chunks_path.exists():
                print(f"  - V1 格式: chunks.json")
                with open(chunks_path, 'r') as f:
                    full_chunks = json.load(f)
                    if isinstance(full_chunks, dict) and 'chunks' in full_chunks:
                        chunks_data = full_chunks.get('chunks', [])
                    else:
                        chunks_data = full_chunks
                print(f"  ✓ 读取到 {len(chunks_data)} 个 chunks (V1)")

        # 读取markdown（V1 和 V2 格式不同）
        print(f"\n📝 读取 markdown...")
        markdown_content = ""
        
        if api_version == "v2":
            # V2: 从 *_extraction.md 读取
            md_files = list(extraction_dir.glob("*_extraction.md"))
            if md_files:
                md_file = md_files[0]
                print(f"  - V2 格式: {md_file.name}")
                with open(md_file, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
                print(f"  ✓ Markdown 长度: {len(markdown_content)} 字符 (V2)")
        else:
            # V1: 从文件名.md 读取
            markdown_path = extraction_dir / f"{metadata['filename'].replace('.pdf', '.md')}"
            if markdown_path.exists():
                print(f"  - V1 格式: {markdown_path.name}")
                with open(markdown_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
                print(f"  ✓ Markdown 长度: {len(markdown_content)} 字符 (V1)")

        # 查找PDF文件（V1 和 V2 路径不同）
        print(f"\n📎 查找 PDF 文件...")
        pdf_filename_pattern = f"*{file_id}*{metadata['filename']}"
        pdf_path = None
        
        # 递归查找
        for pdf_file in base_upload_dir.rglob("*.pdf"):
            if file_id in pdf_file.name and metadata['filename'] in pdf_file.name:
                pdf_path = pdf_file
                print(f"  ✓ 找到 PDF: {pdf_path}")
                break
        
        if not pdf_path:
            print(f"  ⚠️  未找到 PDF 文件")

        print(f"\n" + "="*80)
        print(f"✅ 文档详情提取完成 ({api_version.upper()})")
        print("="*80)
        print(f"  - Filename: {metadata.get('filename')}")
        print(f"  - Chunks: {len(chunks_data)}")
        print(f"  - Markdown: {len(markdown_content)} 字符")
        print(f"  - PDF: {'有' if pdf_path else '无'}")
        print("="*80 + "\n")

        return {
            "status": "success",
            "file_id": file_id,
            "filename": metadata.get('filename'),
            "api_version": api_version,  # ✅ 返回版本信息
            "metadata": metadata.get('metadata', {}) if api_version == "v1" else metadata,
            "extraction_time": metadata.get('extraction_time'),
            "extraction_mode": metadata.get('extraction_mode'),  # V2 特有
            "markdown": markdown_content,
            "chunks": chunks_data,
            "pdf_url": f"/document/{file_id}/pdf" if pdf_path else None,
            "total_chunks": len(chunks_data),
            "total_pages": metadata.get('total_pages', 0) if api_version == "v2" else metadata.get('metadata', {}).get('total_pages', 0),
            "total_images": metadata.get('total_images', 0) if api_version == "v2" else metadata.get('metadata', {}).get('total_images', 0)
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"获取文档详情失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取文档详情失败: {str(e)}")

@app.get("/document/{file_id}/pdf")
async def get_document_pdf(file_id: str):
    """直接返回PDF文件，Content-Disposition设置为inline以便浏览器内嵌显示（支持 V1 和 V2）"""
    try:
        print(f"\n📎 获取 PDF 文件请求: {file_id}")
        
        # 定义基础路径（V1 和 V2 现在都使用同一个路径，从环境变量读取）
        import os
        upload_base = Path(os.getenv(
            "UPLOAD_BASE_DIR",
            "/home/MuyuWorkSpace/01_TrafficProject/Multimodal_RAG_OCR/backend/output/uploads"
        ))
        extraction_base = Path(os.getenv(
            "EXTRACTION_RESULTS_DIR",
            "/home/MuyuWorkSpace/01_TrafficProject/Multimodal_RAG_OCR/backend/output/extraction_results"
        ))
        
        base_dirs_v1 = {
            'upload': upload_base,
            'extraction': extraction_base
        }
        base_dirs_v2 = {
            'upload': upload_base,
            'extraction': extraction_base
        }
        
        # 查找文件目录
        extraction_dir = extraction_base / file_id
        base_upload_dir = upload_base
        
        if not extraction_dir.exists():
            raise HTTPException(status_code=404, detail="文档不存在")
        
        # 通过文件特征判断版本（V1 vs V2）
        has_v2_extraction = len(list(extraction_dir.glob("*_extraction.md"))) > 0
        has_v2_chunked = len(list(extraction_dir.glob("*_chunked_output.json"))) > 0
        has_v1_chunks = (extraction_dir / "chunks.json").exists()
        
        if has_v2_extraction or has_v2_chunked:
            api_version = "v2"
        elif has_v1_chunks:
            api_version = "v1"
        else:
            api_version = "v1"  # 默认V1
        
        print(f"  - 使用版本: {api_version}")
        
        # 读取metadata
        metadata_files = list(extraction_dir.glob("*_metadata.json")) + [extraction_dir / "metadata.json"]
        metadata_path = None
        for mf in metadata_files:
            if mf.exists():
                metadata_path = mf
                break
        
        if not metadata_path:
            raise HTTPException(status_code=404, detail="metadata文件不存在")

        with open(metadata_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)

        # 查找PDF文件
        filename = metadata.get('filename', '')
        pdf_path = None
        
        # 递归查找
        for pdf_file in base_upload_dir.rglob("*.pdf"):
            if file_id in pdf_file.name and filename in pdf_file.name:
                pdf_path = pdf_file
                break

        if not pdf_path or not pdf_path.exists():
            print(f"  ❌ PDF 文件未找到")
            raise HTTPException(status_code=404, detail="PDF文件不存在")

        print(f"  ✓ 找到 PDF: {pdf_path}")

        # 返回PDF，设置为inline模式以便浏览器内嵌显示
        from fastapi.responses import Response
        from urllib.parse import quote
        with open(pdf_path, 'rb') as f:
            pdf_content = f.read()

        # URL encode the filename for proper handling of Chinese characters
        encoded_filename = quote(filename)

        print(f"  ✓ 返回 PDF ({api_version.upper()}): {filename}\n")

        return Response(
            content=pdf_content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
                "Cache-Control": "public, max-age=3600"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"获取PDF文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取PDF文件失败: {str(e)}")

@app.get("/document/{file_id}/images/{image_name}")
async def get_document_image(file_id: str, image_name: str):
    """获取文档提取的图片"""
    try:
        # 使用环境变量中的提取结果目录，避免硬编码路径
        import os
        base_extraction_dir = Path(os.getenv(
            "EXTRACTION_RESULTS_DIR",
            "./output/extraction_results"
        ))

        # 图片路径
        image_path = base_extraction_dir / file_id / "images" / image_name

        if not image_path.exists():
            raise HTTPException(status_code=404, detail="图片不存在")

        # 根据文件扩展名设置media_type
        ext = image_path.suffix.lower()
        media_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        media_type = media_types.get(ext, 'image/png')

        return FileResponse(
            path=str(image_path),
            media_type=media_type
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"获取图片失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取图片失败: {str(e)}")

if __name__ == "__main__":
    host = os.getenv("SERVER_HOST", os.getenv("MILVUS_API_HOST", "127.0.0.1"))
    port = int(os.getenv("SERVER_PORT", "8000"))
    
    print("\n" + "="*60)
    print("Starting Milvus RAG Service")
    print("="*60)
    print(f"Server: http://{host}:{port}")
    print(f"Docs: http://{host}:{port}/docs")
    print("="*60 + "\n")
    
    uvicorn.run(app, host=host, port=port)
