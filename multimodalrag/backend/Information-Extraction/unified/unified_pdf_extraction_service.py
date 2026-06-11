"""
PDF提取服务 - FastAPI接口版本
支持快速模式和精确模式的HTTP API调用
"""
from dataclasses import dataclass
import io
import base64
import asyncio
import json
import re
import tempfile
import shutil
import os
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, Depends 
from fastapi.responses import JSONResponse, FileResponse 
from fastapi.middleware.cors import CORSMiddleware 
from fastapi.staticfiles import StaticFiles 
from pydantic import BaseModel 
from PIL import Image 
from pdf2image import convert_from_bytes
import uvicorn
import httpx
import jwt
import logging
from llm_extraction import PAGES_PER_REQUEST, CONCURRENT_REQUESTS
from dotenv import load_dotenv

# 加载 backend/.env 文件（统一使用 backend/.env）
# 注意：本文件位于 backend/Information-Extraction/unified/
# 因此向上三级即为 backend 目录
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)

# 设置日志
log_dir = Path(__file__).parent.parent.parent / 'logs'
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=log_dir / 'upload.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _load_fast_pdf_dependencies():
    """Load optional AGPL/commercial PDF dependencies only when fast mode runs."""
    try:
        import pymupdf4llm  # type: ignore
        import fitz  # type: ignore
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail=(
                "Fast extraction requires optional PyMuPDF/PyMuPDF4LLM dependencies. "
                "Install multimodalrag/backend/requirements-pdf-agpl.txt only if your "
                "use case complies with AGPL obligations or you have an Artifex "
                "commercial license. Use accurate/VLM mode for the default public setup."
            ),
        ) from exc
    return pymupdf4llm, fitz

# ============ 配置 ============

# 从环境变量读取配置，如果没有则使用默认值
UPLOAD_BASE_DIR = Path(os.getenv(
    "UPLOAD_BASE_DIR",
    # Default to a project-local path for public developer setups.
    "./output/uploads"
))

EXTRACTION_RESULTS_DIR = Path(os.getenv(
    "EXTRACTION_RESULTS_DIR",
    "./output/extraction_results"
))

# 文件大小限制（MB）
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# 服务配置
SERVICE_PORT = int(os.getenv("INFOR_EXTRAC_SERVICE_PORT", "8006"))
SERVICE_HOST = os.getenv("INFOR_EXTRAC_SERVICE_HOST", "127.0.0.1")

# 切分服务配置
CHUNK_SERVICE_HOST = os.getenv("CHUNK_SERVICE_HOST", "localhost")
CHUNK_SERVICE_PORT = int(os.getenv("CHUNK_SERVICE_PORT", "8001"))

# 如果HOST是0.0.0.0，转换为localhost用于HTTP调用
chunk_host_for_url = "localhost" if CHUNK_SERVICE_HOST == "0.0.0.0" else CHUNK_SERVICE_HOST
CHUNKING_SERVICE_URL = f"http://{chunk_host_for_url}:{CHUNK_SERVICE_PORT}"
CHUNKING_SERVICE_ENABLED = os.getenv("CHUNKING_SERVICE_ENABLED", "true").lower() == "true"

# Milvus API服务配置
MILVUS_API_HOST = os.getenv("MILVUS_API_HOST", "localhost")
MILVUS_API_PORT = int(os.getenv("MILVUS_API_PORT", "8000"))

# 如果HOST是0.0.0.0，转换为localhost用于HTTP调用
milvus_api_host_for_url = "localhost" if MILVUS_API_HOST == "0.0.0.0" else MILVUS_API_HOST
MILVUS_API_URL = f"http://{milvus_api_host_for_url}:{MILVUS_API_PORT}"
MILVUS_API_ENABLED = os.getenv("MILVUS_API_ENABLED", "true").lower() == "true"

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
ADMIN_ROLES = {"admin"}


def _require_jwt_secret() -> str:
    if not JWT_SECRET_KEY or JWT_SECRET_KEY in UNSAFE_JWT_SECRETS or len(JWT_SECRET_KEY) < 32:
        raise HTTPException(status_code=500, detail="JWT_SECRET_KEY is not configured safely")
    return JWT_SECRET_KEY

# 创建必要的目录
UPLOAD_BASE_DIR.mkdir(parents=True, exist_ok=True)
EXTRACTION_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# 挂载静态文件目录（MinerU可视化图片）
MINERU_VIZ_DIR = Path(os.getenv(
    "MINERU_VIZ_DIR",
    "./mineru_visualizations"
))
MINERU_VIZ_DIR.mkdir(parents=True, exist_ok=True)


print(f"配置加载完成:")
print(f"  - 上传目录: {UPLOAD_BASE_DIR}")
print(f"  - 提取结果目录: {EXTRACTION_RESULTS_DIR}")
print(f"  - 文件大小限制: {MAX_FILE_SIZE_MB}MB")
print(f"  - 服务地址: {SERVICE_HOST}:{SERVICE_PORT}")
print(f"  - 切分服务: {'启用' if CHUNKING_SERVICE_ENABLED else '禁用'} ({CHUNKING_SERVICE_URL})")
print(f"  - Milvus API服务: {'启用' if MILVUS_API_ENABLED else '禁用'} ({MILVUS_API_URL})")

# ============ 数据模型 ============

class AccurateExtractionRequest(BaseModel):
    """精确模式提取请求"""
    api_key: str
    model_name: str
    model_url: str


class ExtractionResponse(BaseModel):
    """提取响应"""
    success: bool
    message: str
    filename: Optional[str] = None  # 添加文件名字段
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class UploadResponse(BaseModel):
    """文件上传响应"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, str]] = None


@dataclass
class ExtractionResult:
    """提取结果数据类"""
    filename: str = ""  # 添加默认值
    markdown_content: str = ""
    tables: List[Dict[str, Any]] = None
    formulas: List[Dict[str, Any]] = None
    metadata: Dict[str, Any] = None
    token_usage: Dict[str, int] = None
    time_cost: Dict[str, float] = None
    page_images: List[Image.Image] = None
    per_page_results: List[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.tables is None:
            self.tables = []
        if self.formulas is None:
            self.formulas = []
        if self.metadata is None:
            self.metadata = {}
        if self.token_usage is None:
            self.token_usage = {}
        if self.time_cost is None:
            self.time_cost = {}
        if self.page_images is None:
            self.page_images = []
        if self.per_page_results is None:
            self.per_page_results = []


# ============ PDF提取服务 ============

class PDFExtractionService:
    """统一的PDF提取服务"""
    
    def __init__(self):
        self.default_pages_per_request = PAGES_PER_REQUEST  
        self.default_concurrent_requests = CONCURRENT_REQUESTS
        self.default_dpi = 100
    
    async def extract_fast(self, file_path: str, original_filename: Optional[str] = None) -> Dict[str, Any]:
        """快速模式：使用PyMuPDF4LLM提取

        1. 页码标记：在每页开头加 {{第X页}}，方便后续分页处理
        2. 图片提取：提取PDF中的图片（不是截图整页），返回 base64
        3. 给每页生成完整截图
        
        """
        pymupdf4llm, fitz = _load_fast_pdf_dependencies()
        
        # 获取文件名
        filename = original_filename or Path(file_path).name
        
        pdf_path = Path(file_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        temp_dir = Path(tempfile.mkdtemp())
        temp_images_dir = temp_dir / "images"
        temp_images_dir.mkdir(exist_ok=True)
        
        md_data = pymupdf4llm.to_markdown(
            str(pdf_path),
            page_chunks=True,
            write_images=True,
            image_path=str(temp_images_dir),
            image_format="png",
            dpi=150
        )
        
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)
        
        markdown_parts = []
        
        if isinstance(md_data, list):
            for idx, page_data in enumerate(md_data):
                page_num = idx + 1
                if isinstance(page_data, dict):
                    text = page_data.get('text', '')
                else:
                    text = str(page_data)
                
                text = text.replace(str(temp_images_dir.absolute()), "images")
                markdown_parts.append(f"{{{{第{page_num}页}}}}\n{text}\n")
        else:
            text = str(md_data)
            text = text.replace(str(temp_images_dir.absolute()), "images")
            
            if "-----" in text or "---" in text:
                pages = text.split("-----") if "-----" in text else text.split("---")
                for idx, page_text in enumerate(pages):
                    if page_text.strip():
                        page_num = idx + 1
                        markdown_parts.append(f"{{{{第{page_num}页}}}}\n{page_text.strip()}\n")
            else:
                for page_num in range(1, total_pages + 1):
                    markdown_parts.append(f"{{{{第{page_num}页}}}}\n")
                markdown_parts.append(text)
        
        images_data = []
        
        for img_file in sorted(temp_images_dir.glob("*.png")):
            try:
                img = Image.open(img_file)
                buffer = io.BytesIO()
                img.save(buffer, format='PNG')
                buffer.seek(0)
                img_base64 = base64.b64encode(buffer.read()).decode('utf-8')
                
                img_filename = img_file.name
                page_num = 1
                match = re.search(r'(\d+)', img_filename)
                if match:
                    page_num = int(match.group(1))
                
                images_data.append({
                    "filename": img_filename,
                    "base64": img_base64,
                    "page_num": page_num
                })
                                
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"处理图片失败 {img_file.name}: {e}")
        
        for page_num in range(total_pages):
            page = doc[page_num]
            
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                
                buffer = io.BytesIO()
                img.save(buffer, format='PNG')
                buffer.seek(0)
                img_base64 = base64.b64encode(buffer.read()).decode('utf-8')
                
                img_filename = f"page_{page_num + 1}_full.png"
                images_data.append({
                    "filename": img_filename,
                    "base64": img_base64,
                    "page_num": page_num + 1
                })
                
                markdown_parts.append(f"\n![{img_filename}](images/{img_filename})\n")
                pix = None
                
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"截图失败: {e}")
        
        doc.close()
        
        try:
            shutil.rmtree(temp_dir)
        except:
            pass
        
        final_markdown = "".join(markdown_parts)
        
        
        return {
            "filename": filename,  
            "markdown": final_markdown,
            "images": images_data,
            "metadata": {
                "total_pages": total_pages,
                "total_images": len(images_data)
            }
        }
    
    async def extract_accurate(
        self,
        file_path: str,
        api_key: str,
        model_name: str,
        model_url: str,
        original_filename: Optional[str] = None  # 添加原始文件名参数
    ) -> Dict[str, Any]:
        """精确模式:使用LLM提取"""
 
        from llm_extraction import PDFMultimodalExtractor
        
        extractor = PDFMultimodalExtractor(
            model_url=model_url,
            api_key=api_key,
            model_name=model_name,
            pages_per_request=self.default_pages_per_request
        )
        
        # 临时修改文件名（用于显示和结果中）
        if original_filename:
            # 在提取前，可以将原始文件名传递给extractor
            result = await extractor.extract_from_pdf(file_path, original_filename=original_filename)
        else:
            result = await extractor.extract_from_pdf(file_path)
        
        # 直接使用大模型返回的markdown（已包含所有内容和正确的页码）
        # markdown中的 ## 第X页 是大模型输出的，{{第X页}} 是我们添加的标识符
        markdown_parts = []
        
        for page_result in result.per_page_results:
            page_num = page_result['page_num']
            page_markdown = page_result.get('markdown', '')
            
            # 添加页码标识符（用于分隔不同页面）
            markdown_parts.append(f"{{{{第{page_num}页}}}}\n{page_markdown}\n")
        
        final_markdown = "".join(markdown_parts)
        
        total_image_descriptions = sum(len(p.get('images', [])) for p in result.per_page_results)
          
        # 获取文件名（不包含路径）
        filename = original_filename or Path(file_path).name
        
        return {
            "filename": filename,  # 添加文件名字段
            "markdown": final_markdown,
            "images": [],  # 精确模式不返回图片base64，因为图片描述已在markdown中
            "metadata": {
                "total_pages": result.metadata['total_pages'],
                "total_tables": result.metadata['total_tables'],
                "total_formulas": result.metadata['total_formulas'],
                "total_image_descriptions": total_image_descriptions,
                "token_usage": result.token_usage,
                "time_cost": result.time_cost
            }
        }
    
    async def extract_from_pdf(self, pdf_path: str, original_filename: Optional[str] = None) -> ExtractionResult:
        """从PDF文件中提取完整信息"""
        import time
        overall_start = time.time()
        
        # 优先使用原始文件名，否则从路径提取
        filename = original_filename or Path(pdf_path).name
        print(f"filename: {filename}")
        
        print(f"开始处理PDF: {pdf_path}")
        print("="*60)
        
        # PDF转图片
        convert_start = time.time()
        images = self.pdf_to_images(pdf_path)
        self.pdf_convert_time = time.time() - convert_start
        total_pages = len(images)
        print(f"PDF转换完成: {total_pages} 页 (耗时: {self.pdf_convert_time:.2f}秒)")
        
        # 批量处理页面
        per_page_results = []
        all_tables = []
        all_formulas = []
        
        for i in range(0, total_pages, self.pages_per_request):
            batch_images = images[i:i + self.pages_per_request]
            batch_page_nums = list(range(i + 1, min(i + 1 + self.pages_per_request, total_pages + 1)))
            
            image_base64_list = [self.image_to_base64(img) for img in batch_images]
            
            result = await self.call_multimodal_api(
                image_base64_list=image_base64_list,
                page_nums=batch_page_nums,
                total_pages=total_pages
            )
            
            per_page_results.extend(result['per_page_results'])
            all_tables.extend(result.get('tables', []))
            all_formulas.extend(result.get('formulas', []))
        
        # 组装最终markdown
        final_markdown = ""
        for page_result in per_page_results:
            final_markdown += page_result.get('markdown', '') + "\n\n"
        
        # 统计信息
        metadata = {
            "total_pages": total_pages,
            "total_tables": len(all_tables),
            "total_formulas": len(all_formulas),
            "model": self.model_name
        }
        
        token_usage = {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens
        }
        
        self.total_time = time.time() - overall_start
        time_cost = {
            "pdf_convert_time": round(self.pdf_convert_time, 2),
            "api_call_time": round(self.api_call_time, 2),
            "total_time": round(self.total_time, 2)
        }
        
        print("\n" + "="*60)
        print("✓ 提取完成")
        print(f"  总页数: {total_pages}")
        print(f"  表格数: {len(all_tables)}")
        print(f"  公式数: {len(all_formulas)}")
        print(f"  Token使用: {self.total_tokens:,} (提示: {self.total_prompt_tokens:,}, 完成: {self.total_completion_tokens:,})")
        print(f"  耗时: PDF转换 {self.pdf_convert_time:.2f}s + API调用 {self.api_call_time:.2f}s = 总计 {self.total_time:.2f}s")
        print("="*60 + "\n")
        
        return ExtractionResult(
            filename=filename,
            markdown_content=final_markdown,
            tables=all_tables,
            formulas=all_formulas,
            metadata=metadata,
            token_usage=token_usage,
            time_cost=time_cost,
            page_images=images,
            per_page_results=per_page_results
        )


# ============ FastAPI应用 ============

app = FastAPI(
    title="PDF提取服务API",
    description="支持快速模式和精确模式的PDF内容提取",
    version="1.0.0"
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该指定具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/visualizations", StaticFiles(directory=str(MINERU_VIZ_DIR)), name="visualizations")

service = PDFExtractionService()


# ============ 辅助函数 ============

async def require_admin(request: Request) -> str:
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

    role = payload.get("role") or "admin"
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")

    return user_id


def save_extraction_results(file_id: str, filename: str, result_data: Dict[str, Any]) -> Dict[str, str]:
    """
    保存提取结果到本地

    Args:
        file_id: 文件ID
        filename: 原始文件名
        result_data: 提取结果数据（包含markdown和images）

    Returns:
        保存的文件路径字典
    """
    # 创建文件专属目录
    result_dir = EXTRACTION_RESULTS_DIR / file_id
    result_dir.mkdir(parents=True, exist_ok=True)

    # 保存路径字典
    saved_paths = {}

    # 1. 保存 Markdown 文件
    markdown_path = result_dir / f"{Path(filename).stem}.md"
    with open(markdown_path, 'w', encoding='utf-8') as f:
        f.write(result_data.get('markdown', ''))
    saved_paths['markdown'] = str(markdown_path)

    # 2. 保存图片
    images = result_data.get('images', [])
    if images:
        images_dir = result_dir / "images"
        images_dir.mkdir(exist_ok=True)

        saved_images = []
        for img_data in images:
            img_filename = img_data.get('filename', f"image_{img_data.get('page_num', 0)}.png")
            img_path = images_dir / img_filename

            # 解码base64并保存
            img_base64 = img_data.get('base64', '')
            if img_base64:
                img_bytes = base64.b64decode(img_base64)
                with open(img_path, 'wb') as f:
                    f.write(img_bytes)
                saved_images.append({
                    'filename': img_filename,
                    'path': str(img_path),
                    'page_num': img_data.get('page_num')
                })

        saved_paths['images'] = saved_images

    # 3. 保存元数据
    metadata_path = result_dir / "metadata.json"
    metadata = {
        'file_id': file_id,
        'filename': filename,
        'extraction_time': datetime.now().isoformat(),
        'metadata': result_data.get('metadata', {}),
        'saved_paths': {
            'markdown': str(markdown_path),
            'images_dir': str(result_dir / "images") if images else None
        }
    }
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    saved_paths['metadata'] = str(metadata_path)

    return saved_paths


async def call_chunking_service(
    markdown: str,
    filename: str,
    chunking_method: str = "header_recursive",
    chunk_size: int = 1500,
    chunk_overlap: int = 200,
    max_page_span: int = 3
) -> Dict[str, Any]:
    """
    调用切分服务

    Args:
        markdown: Markdown文本
        filename: 文件名
        chunking_method: 切分方法（header_recursive 或 markdown_only）
        chunk_size: 目标chunk大小
        chunk_overlap: chunk重叠长度
        max_page_span: 最大跨页数

    Returns:
        切分结果
    """
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                f"{CHUNKING_SERVICE_URL}/chunk",
                json={
                    "markdown": markdown,
                    "filename": filename,
                    "config": {
                        "method": chunking_method,
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "max_page_span": max_page_span,
                        "add_bridges": True
                    }
                }
            )
            response.raise_for_status()
            result = response.json()

            if result.get('success'):
                return result.get('data', {})
            else:
                raise Exception(f"切分服务返回失败: {result.get('message')}")

    except httpx.HTTPError as e:
        raise Exception(f"切分服务调用失败: {str(e)}")


def save_chunking_results(file_id: str, chunking_data: Dict[str, Any]) -> str:
    """
    保存切分结果到本地

    Args:
        file_id: 文件ID
        chunking_data: 切分结果数据

    Returns:
        保存的chunks.json文件路径
    """
    result_dir = EXTRACTION_RESULTS_DIR / file_id
    result_dir.mkdir(parents=True, exist_ok=True)

    chunks_path = result_dir / "chunks.json"
    with open(chunks_path, 'w', encoding='utf-8') as f:
        json.dump(chunking_data, f, ensure_ascii=False, indent=2)

    return str(chunks_path)

def _resolve_auth_header(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization")
    if auth_header:
        return auth_header

    token = request.query_params.get("token")
    if token:
        return f"Bearer {token}"
    return None


def _build_auth_headers(auth_header: Optional[str]) -> Dict[str, str]:
    if auth_header:
        return {"Authorization": auth_header}
    return {}

async def call_milvus_api(
    file_id: str,
    filename: str,
    chunks: List[Dict[str, Any]],
    knowledge_base_id: Optional[str] = None,
    auth_header: Optional[str] = None
) -> Dict[str, Any]:
    """
    调用Milvus API服务存储chunks到向量数据库

    Args:
        file_id: 文件ID
        filename: 文件名
        chunks: chunk列表
        knowledge_base_id: 知识库ID（可选）

    Returns:
        存储结果
    """

    try:
        # 转换chunks为Milvus API期望的格式
        converted_chunks = []
        for i, chunk in enumerate(chunks):
            # 获取文本内容（切分服务返回的字段名是"text"）
            chunk_text = chunk.get("text", "")

            converted_chunk = {
                "text": chunk_text,
                "page_start": chunk.get("page_start", 1),
                "page_end": chunk.get("page_end", 1),
                "pages": chunk.get("pages", [1]),
                "text_length": chunk.get("text_length", len(chunk_text)),
                "continued": chunk.get("continued", False),
                "cross_page_bridge": chunk.get("cross_page_bridge", False),
                "is_table_like": chunk.get("is_table_like", False),
                "chunk_index": i,
                # 添加额外的metadata
                "headers": chunk.get("headers", []),
                "file_id": file_id
            }
            converted_chunks.append(converted_chunk)

        # 构建file_data格式（符合Milvus API的UploadKBRequest）
        file_data = {
            "filename": filename,
            "data": {
                "chunks": converted_chunks,
                "metadata": {
                    "file_id": file_id,
                    "knowledge_base_id": knowledge_base_id or "default",
                    "total_chunks": len(converted_chunks)
                }
            }
        }

        collection_name = knowledge_base_id or "default_collection"

        headers = _build_auth_headers(auth_header)
        async with httpx.AsyncClient(timeout=None) as client:
            # 调用 Milvus API 的 /upload_json 端点
            response = await client.post(
                f"{MILVUS_API_URL}/upload_json",
                json={
                    "collection_name": collection_name,
                    "file_data": file_data
                },
                headers=headers if headers else None
            )
            response.raise_for_status()
            result = response.json()

            # 检查响应状态
            if result.get('status') == 'success':
                chunks_count = result.get('chunks_count', len(chunks))

                return {
                    "status": "completed",
                    "inserted_count": chunks_count,
                    "file_id": result.get('file_id'),
                    "collection_name": collection_name
                }
            else:
                raise Exception(f"Milvus API返回失败: {result.get('message', 'unknown error')}")

    except httpx.HTTPError as e:
        print(f"✗ 调用Milvus API失败: {e}")
        raise Exception(f"Milvus API调用失败: {str(e)}")
    except Exception as e:
        print(f"✗ 存储到Milvus失败: {e}")
        raise


@app.get("/")
async def root():
    """健康检查"""
    return {
        "status": "running",
        "service": "PDF Extraction API",
        "version": "1.0.0"
    }


@app.post("/api/v1/files/upload", response_model=UploadResponse, dependencies=[Depends(require_admin)])
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    knowledge_base_id: Optional[str] = Form(None),
    auto_extract: bool = Form(False),
    extraction_mode: str = Form("fast"),
    auto_chunk: bool = Form(False),
    chunking_method: str = Form("header_recursive"),
    chunk_size: int = Form(1500),
    chunk_overlap: int = Form(200),
    max_page_span: int = Form(3)
):
    """
    上传 PDF 文件并可选自动提取和切分

    Args:
        file: PDF 文件
        knowledge_base_id: 知识库 ID（可选）
        auto_extract: 是否自动提取内容（默认 False）
        extraction_mode: 提取模式，"fast" 或 "accurate"（仅当 auto_extract=True 时有效）
        auto_chunk: 是否自动切分（默认 False，需要 auto_extract=True）
        chunking_method: 切分方法，"header_recursive" 或 "markdown_only"
        chunk_size: 目标chunk大小（默认 1500）
        chunk_overlap: chunk重叠长度（默认 200）
        max_page_span: 最大跨页数（默认 3）

    Returns:
        上传结果，包含文件信息、提取结果和切分结果（如果启用）
    """
    auth_header = _resolve_auth_header(request)
    logger.info(f"[V1] Upload started: file={file.filename}, kb_id={knowledge_base_id}, auto_extract={auto_extract}, extraction_mode={extraction_mode}")
    try:
        # 1. 验证文件类型
        if not file.filename or not file.filename.lower().endswith('.pdf'):
            return UploadResponse(
                success=False,
                message="文件上传失败",
                error={
                    "code": "INVALID_FILE_TYPE",
                    "message": "仅支持 PDF 格式文件"
                }
            )

        # 2. 读取文件内容
        content = await file.read()
        file_size = len(content)

        # 3. 验证文件大小
        if file_size > MAX_FILE_SIZE:
            return UploadResponse(
                success=False,
                message="文件上传失败",
                error={
                    "code": "FILE_TOO_LARGE",
                    "message": f"文件大小超过限制（最大 {MAX_FILE_SIZE // 1024 // 1024}MB）"
                }
            )

        # 4. 生成文件 ID 和保存路径
        file_id = f"file_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
        date_path = datetime.now().strftime('%Y/%m/%d')
        upload_dir = UPLOAD_BASE_DIR / date_path
        upload_dir.mkdir(parents=True, exist_ok=True)

        # 保存文件，使用原始文件名
        safe_filename = Path(file.filename).name
        file_path = upload_dir / f"{file_id}_{safe_filename}"

        # 5. 保存文件到本地
        with open(file_path, 'wb') as f:
            f.write(content)
        logger.info(f"[V1] File saved: file_id={file_id}, path={file_path}, size={file_size}")

        # 6. 准备返回数据
        response_data = {
            "file_id": file_id,
            "filename": safe_filename,
            "file_size": file_size,
            "file_path": str(file_path),
            "knowledge_base_id": knowledge_base_id,
            "upload_time": datetime.now().isoformat()
        }

        # 7. 如果启用自动提取，执行提取操作
        if auto_extract:
            logger.info(f"[V1] Starting extraction: mode={extraction_mode}, file_id={file_id}")
            try:
                if extraction_mode == "fast":
                    extraction_result = await service.extract_fast(
                        file_path=str(file_path),
                        original_filename=safe_filename
                    )
                elif extraction_mode == "accurate" or extraction_mode == "vlm":
                    # accurate/vlm 模式，使用环境变量中的 API 配置
                    api_key = os.getenv("API_KEY")
                    model_name = os.getenv("MODEL_NAME")
                    model_url = os.getenv("MODEL_URL")

                    if not all([api_key, model_name, model_url]):
                        return UploadResponse(
                            success=False,
                            message="自动提取失败",
                            error={
                                "code": "MISSING_API_CONFIG",
                                "message": "精确模式需要在 .env 中配置 API_KEY, MODEL_NAME, MODEL_URL"
                            }
                        )

                    extraction_result = await service.extract_accurate(
                        file_path=str(file_path),
                        api_key=api_key,
                        model_name=model_name,
                        model_url=model_url,
                        original_filename=safe_filename
                    )
                else:
                    return UploadResponse(
                        success=False,
                        message="自动提取失败",
                        error={
                            "code": "INVALID_MODE",
                            "message": f"不支持的提取模式: {extraction_mode}，仅支持 'fast' 或 'accurate'"
                        }
                    )

                # 8. 保存提取结果到本地
                saved_paths = save_extraction_results(
                    file_id=file_id,
                    filename=safe_filename,
                    result_data=extraction_result
                )

                # 将提取结果添加到响应中
                response_data['extraction'] = {
                    'status': 'completed',
                    'mode': extraction_mode,
                    'total_pages': extraction_result.get('metadata', {}).get('total_pages'),
                    'total_images': extraction_result.get('metadata', {}).get('total_images'),
                    'markdown_path': saved_paths['markdown'],
                    'images_dir': str(EXTRACTION_RESULTS_DIR / file_id / "images"),
                    'saved_images_count': len(saved_paths.get('images', [])),
                    'markdown': extraction_result.get('markdown', ''),
                    'filename': extraction_result.get('filename', safe_filename),
                    'metadata': extraction_result.get('metadata', {})
                }
                logger.info(f"[V1] Extraction completed: file_id={file_id}, pages={extraction_result.get('metadata', {}).get('total_pages')}, images={extraction_result.get('metadata', {}).get('total_images')}")

                # 9. 如果启用自动切分，执行切分操作
                if auto_chunk and CHUNKING_SERVICE_ENABLED:
                    logger.info(f"[V1] Starting chunking: method={chunking_method}, file_id={file_id}")
                    try:
                        chunking_result = await call_chunking_service(
                            markdown=extraction_result.get('markdown', ''),
                            filename=safe_filename,
                            chunking_method=chunking_method,
                            chunk_size=chunk_size,
                            chunk_overlap=chunk_overlap,
                            max_page_span=max_page_span
                        )

                        # 保存切分结果
                        chunks_path = save_chunking_results(
                            file_id=file_id,
                            chunking_data=chunking_result
                        )

                        # 添加切分结果到响应
                        response_data['chunking'] = {
                            'status': 'completed',
                            'method': chunking_method,
                            'total_chunks': chunking_result.get('chunk_stats', {}).get('total_chunks'),
                            'cross_page_chunks': chunking_result.get('chunk_stats', {}).get('cross_page_chunks'),
                            'avg_chunk_length': chunking_result.get('chunk_stats', {}).get('avg_chunk_length'),
                            'chunks_path': chunks_path,
                            'chunks': chunking_result.get('chunks', []),
                            'chunk_stats': chunking_result.get('chunk_stats', {})
                        }
                        logger.info(f"[V1] Chunking completed: file_id={file_id}, total_chunks={chunking_result.get('chunk_stats', {}).get('total_chunks')}")

                        # 10. 如果启用了Milvus API，存储chunks到向量数据库
                        if MILVUS_API_ENABLED:
                            logger.info(f"[V1] Starting Milvus storage: file_id={file_id}, kb_id={knowledge_base_id}")
                            try:
                                chunks = chunking_result.get('chunks', [])
                                storage_result = await call_milvus_api(
                                    file_id=file_id,
                                    filename=safe_filename,
                                    chunks=chunks,
                                    knowledge_base_id=knowledge_base_id,
                                    auth_header=auth_header
                                )

                                # 添加存储结果到响应
                                response_data['storage'] = {
                                    'status': 'completed',
                                    'inserted_count': storage_result.get('inserted_count'),
                                    'collection_name': storage_result.get('collection_name'),
                                    'storage_file_id': storage_result.get('file_id')
                                }
                                logger.info(f"[V1] Milvus storage completed: file_id={file_id}, inserted={storage_result.get('inserted_count')}")

                                print(f"✓ 成功存储 {storage_result.get('inserted_count')} 个chunks到Milvus")

                            except Exception as storage_error:
                                logger.error(f"[V1] Milvus storage failed: file_id={file_id}, error={storage_error}")
                                print(f"✗ 存储到Milvus失败: {storage_error}")
                                import traceback
                                traceback.print_exc()
                                response_data['storage'] = {
                                    'status': 'failed',
                                    'error': str(storage_error)
                                }
                        else:
                            response_data['storage'] = {
                                'status': 'skipped',
                                'message': 'Milvus API服务未启用'
                            }

                    except Exception as chunk_error:
                        logger.error(f"[V1] Chunking failed: file_id={file_id}, error={chunk_error}")
                        print(f"✗ 自动切分失败: {chunk_error}")
                        import traceback
                        traceback.print_exc()
                        response_data['chunking'] = {
                            'status': 'failed',
                            'error': str(chunk_error)
                        }
                elif auto_chunk and not CHUNKING_SERVICE_ENABLED:
                    response_data['chunking'] = {
                        'status': 'skipped',
                        'message': '切分服务未启用'
                    }

            except Exception as e:
                logger.error(f"[V1] Extraction failed: file_id={file_id}, error={e}")
                print(f"✗ 自动提取失败: {e}")
                import traceback
                traceback.print_exc()
                response_data['extraction'] = {
                    'status': 'failed',
                    'error': str(e)
                }

        # 构建成功消息
        success_msg = "文件上传成功"
        if auto_extract:
            success_msg += "并已完成提取"
        if auto_chunk and CHUNKING_SERVICE_ENABLED:
            success_msg += "和切分"
        if response_data.get('storage', {}).get('status') == 'completed':
            success_msg += "，已入库"

        logger.info(f"[V1] Upload completed successfully: file_id={file_id}, {success_msg}")
        return UploadResponse(
            success=True,
            message=success_msg,
            data=response_data
        )

    except Exception as e:
        logger.error(f"[V1] Upload failed: file={file.filename}, error={e}")
        print(f"文件上传失败: {e}")
        import traceback
        traceback.print_exc()

        return UploadResponse(
            success=False,
            message="文件上传失败",
            error={
                "code": "UPLOAD_FAILED",
                "message": str(e)
            }
        )

# ============ v2 API - OCR 2.0 ============

@app.post("/api/v2/files/upload", response_model=UploadResponse, dependencies=[Depends(require_admin)])
async def upload_pdf_v2(
    request: Request,
    file: UploadFile = File(...),
    knowledge_base_id: Optional[str] = Form(None),
    auto_extract: bool = Form(False),
    extraction_mode: str = Form("mineru"),  # mineru, paddleocr, deepseek
    auto_chunk: bool = Form(False),
    chunking_method: str = Form("ocr_aware"),  # ocr_aware, layout_based
    chunk_size: int = Form(1500),
    chunk_overlap: int = Form(200),
    max_page_span: int = Form(3)
):
    """
    上传 PDF 文件并可选自动提取和切分 (OCR 2.0版本)
    
    Args:
        file: PDF 文件
        knowledge_base_id: 知识库 ID（可选）
        auto_extract: 是否自动提取内容（默认 False）
        extraction_mode: OCR方法，"mineru"、"paddleocr" 或 "deepseek"
        auto_chunk: 是否自动切分（默认 False，需要 auto_extract=True）
        chunking_method: 切分方法，"ocr_aware" 或 "layout_based"
        chunk_size: 目标chunk大小（默认 1500）
        chunk_overlap: chunk重叠长度（默认 200）
        max_page_span: 最大跨页数（默认 3）
    
    Returns:
        上传结果，包含文件信息、提取结果和切分结果（如果启用）
    """
    auth_header = _resolve_auth_header(request)
    logger.info(f"[V2] Upload started: file={file.filename}, kb_id={knowledge_base_id}, auto_extract={auto_extract}, extraction_mode={extraction_mode}")
    try:
        # 导入OCR v2提取器
        from ocr_v2_extractors import create_ocr_extractor

        # 1. 验证文件类型
        if not file.filename or not file.filename.lower().endswith('.pdf'):
            return UploadResponse(
                success=False,
                message="文件上传失败",
                error={
                    "code": "INVALID_FILE_TYPE",
                    "message": "仅支持 PDF 格式文件"
                }
            )

        # 2. 读取文件内容
        content = await file.read()
        file_size = len(content)

        # 3. 验证文件大小
        if file_size > MAX_FILE_SIZE:
            return UploadResponse(
                success=False,
                message="文件上传失败",
                error={
                    "code": "FILE_TOO_LARGE",
                    "message": f"文件大小超过限制 ({MAX_FILE_SIZE_MB}MB)"
                }
            )

        # 4. 生成文件ID和保存路径
        file_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_filename = Path(file.filename).name

        # 如果指定了知识库，保存到知识库目录下，否则保存到默认目录
        if knowledge_base_id:
            upload_dir = UPLOAD_BASE_DIR / knowledge_base_id
        else:
            upload_dir = UPLOAD_BASE_DIR / "default"

        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{timestamp}_{file_id}_{safe_filename}"

        # 5. 保存文件
        print(f"✓ 保存文件到: {file_path}")
        with open(file_path, 'wb') as f:
            f.write(content)
        logger.info(f"[V2] File saved: file_id={file_id}, path={file_path}, size={file_size}")

        # 构建响应数据
        response_data = {
            'file': {
                'file_id': file_id,
                'filename': safe_filename,
                'path': str(file_path),
                'size': file_size,
                'size_mb': round(file_size / (1024 * 1024), 2),
                'upload_time': datetime.now().isoformat(),
                'knowledge_base_id': knowledge_base_id
            }
        }

        # 6. 如果启用了自动提取
        if auto_extract:
            logger.info(f"[V2] Starting extraction: mode={extraction_mode}, file_id={file_id}")
            print(f"✓ 开始OCR 2.0提取，方法: {extraction_mode}")
            try:
                # 创建OCR提取器
                extractor = create_ocr_extractor(extraction_mode)
                
                # 执行OCR提取
                extraction_result = await extractor.extract(file_path)
                
                # 保存提取结果
                extraction_dir = EXTRACTION_RESULTS_DIR / file_id
                extraction_dir.mkdir(parents=True, exist_ok=True)
                
                # 1. 保存 Markdown 文件
                result_file = extraction_dir / f"{extraction_mode}_extraction.md"
                with open(result_file, 'w', encoding='utf-8') as f:
                    f.write(extraction_result['markdown'])
                
                # 2. 保存完整的原始 API 返回数据（output.json）
                raw_data = extraction_result.get('raw_data', {})
                if raw_data:
                    output_json_file = extraction_dir / f"{extraction_mode}_output.json"
                    with open(output_json_file, 'w', encoding='utf-8') as f:
                        # 构建类似 MinerU demo 的 output.json 格式
                        output_data = {
                            'backend': raw_data.get('backend', extraction_mode),
                            'version': raw_data.get('version', '2.5.4'),
                            'results': {
                                safe_filename.replace('.pdf', ''): raw_data
                            }
                        }
                        json.dump(output_data, f, ensure_ascii=False, indent=2)
                    print(f"  ✓ 保存完整数据: {output_json_file}")

                # 2.5. 提取并保存 base64 图片到物理文件
                images_dict = raw_data.get('images', {})
                if images_dict:
                    images_dir = extraction_dir / "images"
                    images_dir.mkdir(exist_ok=True)

                    saved_image_count = 0
                    for img_path_str, img_base64 in images_dict.items():
                        try:
                            # 提取文件名 (e.g., "images/xxx.png" -> "xxx.png")
                            img_filename = Path(img_path_str).name
                            img_file_path = images_dir / img_filename

                            # 解码 base64 并保存
                            img_bytes = base64.b64decode(img_base64)
                            with open(img_file_path, 'wb') as f:
                                f.write(img_bytes)
                            saved_image_count += 1
                        except Exception as e:
                            print(f"  ⚠ 保存图片失败 {img_filename}: {e}")

                    if saved_image_count > 0:
                        print(f"  ✓ 保存图片文件: {saved_image_count} 张 -> {images_dir}")

                # 3. 分开保存 layout_info 的各个部分（如果存在）
                layout_info = extraction_result.get('layout_info', {})
                saved_layout_files = {}
                
                if layout_info:
                    # 保存 middle_json
                    if layout_info.get('middle_json'):
                        middle_json_file = extraction_dir / f"{extraction_mode}_middle_json.json"
                        with open(middle_json_file, 'w', encoding='utf-8') as f:
                            json.dump(layout_info['middle_json'], f, ensure_ascii=False, indent=2)
                        saved_layout_files['middle_json'] = str(middle_json_file)
                    
                    # 保存 model_output
                    if layout_info.get('model_output'):
                        model_output_file = extraction_dir / f"{extraction_mode}_model_output.json"
                        with open(model_output_file, 'w', encoding='utf-8') as f:
                            json.dump(layout_info['model_output'], f, ensure_ascii=False, indent=2)
                        saved_layout_files['model_output'] = str(model_output_file)
                    
                    # 保存 content_list
                    if layout_info.get('content_list'):
                        content_list_file = extraction_dir / f"{extraction_mode}_content_list.json"
                        with open(content_list_file, 'w', encoding='utf-8') as f:
                            json.dump(layout_info['content_list'], f, ensure_ascii=False, indent=2)
                        saved_layout_files['content_list'] = str(content_list_file)
                
                # 4. 保存轻量级元数据（不包含大的 layout_info 内容）
                metadata_file = extraction_dir / f"{extraction_mode}_metadata.json"
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        'file_id': file_id,
                        'filename': safe_filename,
                        'extraction_mode': extraction_mode,
                        'total_pages': extraction_result['total_pages'],
                        'total_images': extraction_result['total_images'],
                        'extraction_time': datetime.now().isoformat(),
                        'layout_files': saved_layout_files  # 只保存文件路径引用
                    }, f, ensure_ascii=False, indent=2)
                
                print(f"✓ OCR 2.0提取完成 ({extraction_mode})")
                print(f"  - 总页数: {extraction_result['total_pages']}")
                print(f"  - 图片数: {extraction_result['total_images']}")
                
                response_data['extraction'] = {
                    'status': 'completed',
                    'method': extraction_mode,
                    'total_pages': extraction_result['total_pages'],
                    'total_images': extraction_result['total_images'],
                    'markdown_path': str(result_file),
                    'metadata_path': str(metadata_file),
                    'visualization_paths': extraction_result.get('visualization_paths', []),
                    # V2.0: 添加实际的 markdown 内容（供前端直接显示）
                    'markdown': extraction_result.get('markdown', ''),
                    # V2.0: 添加 OCR raw_data（可选，根据需求）
                    'backend': extraction_mode,
                    'version': '2.0'
                }

                # 7. 如果启用了自动切分（OCR 2.0 使用新的切分流程）
                if auto_chunk and CHUNKING_SERVICE_ENABLED:
                    print("\n" + "="*80)
                    print(f"🔧 OCR 2.0 自动切分")
                    print("="*80)
                    print(f"  - 切分方法: {chunking_method} (V2 方法)")
                    print(f"  - Chunk 大小: {chunk_size}")
                    print(f"  - 重叠: {chunk_overlap}")
                    print(f"  - 最大跨页: {max_page_span}")
                    
                    try:
                        # 构建完整的 output.json 格式（符合 /chunk/v2 端点的要求）
                        result_key = safe_filename.replace('.pdf', '')
                        output_json_for_chunk = {
                            'backend': raw_data.get('backend', extraction_mode),
                            'version': raw_data.get('version', '2.5.4'),
                            'results': {
                                result_key: raw_data
                            }
                        }
                        
                        print(f"\n📦 准备调用切分服务:")
                        print(f"  - URL: {CHUNKING_SERVICE_URL}/chunk/v2")
                        print(f"  - Result Key: {result_key}")
                        print(f"  - Output JSON 包含字段: {list(raw_data.keys())}")
                        print(f"  - 是否有 images: {'images' in raw_data}")
                        print(f"  - 是否有 page_images: {'page_images' in raw_data}")
                        
                        # 调用切分服务 v2 端点
                        chunk_url = f"{CHUNKING_SERVICE_URL}/chunk/v2"
                        chunk_payload = {
                            'output_json': output_json_for_chunk,
                            'result_key': result_key,
                            'file_id': file_id,  # 传递file_id用于构建图片URL
                            'config': {
                                'method': chunking_method,  # ocr_aware 或 layout_based
                                'chunk_size': chunk_size,
                                'chunk_overlap': chunk_overlap,
                                'merge_tolerance': 0.2,
                                'max_page_span': max_page_span,
                                'bridge_span': 150,
                                'add_bridges': True
                            }
                        }

                        print(f"\n🌐 调用切分服务...")
                        async with httpx.AsyncClient(timeout=None) as client:
                            chunk_response = await client.post(chunk_url, json=chunk_payload)
                        
                        print(f"  - 响应状态码: {chunk_response.status_code}")

                        if chunk_response.status_code == 200:
                            chunked_output_json = chunk_response.json()
                            
                            # 从返回的 output.json 中提取 chunks 和统计信息
                            result_data = chunked_output_json.get('results', {}).get(result_key, {})
                            chunks = result_data.get('chunks', [])
                            chunk_stats = result_data.get('chunk_stats', {})
                            
                            # 保存切分后的完整 output.json
                            chunked_output_file = extraction_dir / f"{extraction_mode}_chunked_output.json"
                            with open(chunked_output_file, 'w', encoding='utf-8') as f:
                                json.dump(chunked_output_json, f, ensure_ascii=False, indent=2)
                            
                            print("\n" + "="*80)
                            print("✅ OCR 2.0 切分完成")
                            print("="*80)
                            print(f"📊 切分统计:")
                            print(f"  - 总 chunks: {len(chunks)}")
                            print(f"  - 跨页桥接: {chunk_stats.get('bridge_chunks', 0)}")
                            print(f"  - 跨页普通: {chunk_stats.get('cross_page_chunks', 0)}")
                            print(f"  - 单页块: {chunk_stats.get('single_page_chunks', 0)}")
                            print(f"  - 表格块: {chunk_stats.get('table_chunks', 0)}")
                            print(f"  - 平均长度: {chunk_stats.get('avg_chunk_length', 0):.0f} 字符")
                            
                            print(f"\n💾 保存文件:")
                            print(f"  - 切分结果: {chunked_output_file}")
                            
                            # 打印前2个 chunks 的详细信息
                            print(f"\n📝 前 2 个 chunks 预览:")
                            for i, chunk in enumerate(chunks[:2]):
                                print(f"  Chunk {i+1}:")
                                print(f"    - 页码: {chunk['page_start']}-{chunk['page_end']}")
                                print(f"    - 长度: {chunk['text_length']} 字符")
                                print(f"    - 跨页: {chunk.get('continued', False)}")
                                print(f"    - 桥接: {chunk.get('cross_page_bridge', False)}")
                                print(f"    - 文本: {chunk['text'][:60]}...")
                            print("="*80 + "\n")
                            
                            response_data['chunking'] = {
                                'status': 'completed',
                                'method': chunking_method,
                                'total_chunks': len(chunks),
                                'chunk_stats': chunk_stats,
                                'chunked_output_path': str(chunked_output_file),
                                # V2.0: 添加实际的 chunks 数据（供前端直接显示）
                                'chunks': chunks
                            }

                            # 8. 如果有知识库ID且启用了Milvus，存储到向量数据库（使用 V2 端点）
                            if knowledge_base_id and MILVUS_API_ENABLED:
                                print("\n" + "="*80)
                                print(f"🗄️  OCR 2.0 向量数据库存储")
                                print("="*80)
                                print(f"  - Collection: {knowledge_base_id}")
                                print(f"  - Chunks数量: {len(chunks)}")
                                
                                try:
                                    # 调用 Milvus API V2 端点
                                    storage_url = f"{MILVUS_API_URL}/upload_json/v2"
                                    storage_payload = {
                                        'collection_name': knowledge_base_id,
                                        'file_id': file_id,  # ✅ 传递原始 file_id
                                        'output_json': chunked_output_json,  # 传递完整的 output.json
                                        'result_key': result_key
                                    }
                                    
                                    print(f"\n🌐 调用 Milvus API V2...")
                                    print(f"  - URL: {storage_url}")
                                    print(f"  - Collection: {knowledge_base_id}")
                                    print(f"  - File ID: {file_id}")
                                    print(f"  - Result Key: {result_key}")
                                    
                                    headers = _build_auth_headers(auth_header)
                                    async with httpx.AsyncClient(timeout=None) as client:
                                        storage_response = await client.post(
                                            storage_url,
                                            json=storage_payload,
                                            headers=headers if headers else None
                                        )
                                    
                                    print(f"  - 响应状态码: {storage_response.status_code}")
                                    
                                    if storage_response.status_code == 200:
                                        storage_result = storage_response.json()
                                        
                                        print("\n" + "="*80)
                                        print("✅ OCR 2.0 数据存储完成")
                                        print("="*80)
                                        print(f"  - 状态: {storage_result.get('status')}")
                                        print(f"  - 插入数量: {storage_result.get('chunks_count')}")
                                        print(f"  - Backend: {storage_result.get('backend')}")
                                        print(f"  - Version: {storage_result.get('version')}")
                                        print("="*80 + "\n")
                                        
                                        response_data['storage'] = {
                                            'status': 'completed',
                                            'collection_id': knowledge_base_id,
                                            'inserted_count': storage_result.get('chunks_count'),
                                            'backend': storage_result.get('backend'),
                                            'version': storage_result.get('version')
                                        }
                                    else:
                                        error_text = storage_response.text[:500] if storage_response.text else "No response"
                                        print(f"\n❌ Milvus API 返回错误:")
                                        print(f"  - 状态码: {storage_response.status_code}")
                                        print(f"  - 错误: {error_text}")
                                        response_data['storage'] = {
                                            'status': 'failed',
                                            'error': f'Milvus API 返回错误: {storage_response.status_code}, {error_text}'
                                        }

                                except Exception as storage_error:
                                    print(f"\n❌ 存储异常: {storage_error}")
                                    import traceback
                                    traceback.print_exc()
                                    response_data['storage'] = {
                                        'status': 'failed',
                                        'error': str(storage_error)
                                    }
                            elif not MILVUS_API_ENABLED:
                                response_data['storage'] = {
                                    'status': 'skipped',
                                    'message': 'Milvus API服务未启用'
                                }

                        else:
                            error_text = chunk_response.text[:500] if chunk_response.text else "No response body"
                            print(f"\n❌ 切分服务返回错误:")
                            print(f"  - 状态码: {chunk_response.status_code}")
                            print(f"  - 错误信息: {error_text}")
                            response_data['chunking'] = {
                                'status': 'failed',
                                'error': f'切分服务返回错误: {chunk_response.status_code}, {error_text}'
                            }

                    except Exception as chunk_error:
                        print(f"\n❌ 自动切分异常:")
                        print(f"  - 错误: {chunk_error}")
                        import traceback
                        traceback.print_exc()
                        response_data['chunking'] = {
                            'status': 'failed',
                            'error': str(chunk_error)
                        }
                elif auto_chunk and not CHUNKING_SERVICE_ENABLED:
                    response_data['chunking'] = {
                        'status': 'skipped',
                        'message': '切分服务未启用'
                    }

            except Exception as e:
                print(f"✗ OCR 2.0提取失败: {e}")
                import traceback
                traceback.print_exc()
                response_data['extraction'] = {
                    'status': 'failed',
                    'method': extraction_mode,
                    'error': str(e)
                }

        # 构建成功消息
        success_msg = "文件上传成功 (OCR 2.0)"
        if auto_extract:
            success_msg += f"并已完成{extraction_mode}提取"
        if auto_chunk and CHUNKING_SERVICE_ENABLED:
            success_msg += "和切分"
        if response_data.get('storage', {}).get('status') == 'completed':
            success_msg += "，已入库"

        logger.info(f"[V2] Upload completed successfully: file_id={file_id}, {success_msg}")
        return UploadResponse(
            success=True,
            message=success_msg,
            data=response_data
        )

    except Exception as e:
        logger.error(f"[V2] Upload failed: file={file.filename}, error={e}")
        print(f"文件上传失败 (OCR 2.0): {e}")
        import traceback
        traceback.print_exc()

        return UploadResponse(
            success=False,
            message="文件上传失败 (OCR 2.0)",
            error={
                "code": "UPLOAD_FAILED",
                "message": str(e)
            }
        )


@app.post("/extract/fast", response_model=ExtractionResponse, dependencies=[Depends(require_admin)])
async def extract_fast(file: UploadFile = File(...)):
    """
    快速模式提取
    
    - **file**: PDF文件
    
    返回markdown内容和提取的图片（base64编码）
    """
    temp_file = None
    try:
        # 打印上传的文件名
        print(f"收到文件: {file.filename}")
        
        # 保存上传的文件到临时位置
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        content = await file.read()
        temp_file.write(content)
        temp_file.close()
        
        # 执行提取
        result = await service.extract_fast(
            file_path=temp_file.name, 
            original_filename=file.filename  # 添加这一行
        )
        
        # 只返回文件名部分，不包含路径
        filename_only = Path(file.filename).name if file.filename else None
        
        return ExtractionResponse(
            success=True,
            message="快速提取成功",
            filename=filename_only,  # 只返回文件名部分
            data=result
        )
        
    except Exception as e:
        print(f"快速提取失败: {e}")
        import traceback
        traceback.print_exc()
        # 只返回文件名部分，不包含路径
        filename_only = Path(file.filename).name if file.filename else None
        
        return ExtractionResponse(
            success=False,
            message="快速提取失败",
            filename=filename_only,  # 只返回文件名部分
            error=str(e)
        )
    finally:
        # 清理临时文件
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except:
                pass


@app.post("/extract/accurate", response_model=ExtractionResponse, dependencies=[Depends(require_admin)])
async def extract_accurate(
    file: UploadFile = File(...),
    api_key: str = Form(...),
    model_name: str = Form(...),
    model_url: str = Form(...),
    save_file: bool = Form(True),  # 是否保存文件到本地
    knowledge_base_id: Optional[str] = Form(None)  # 知识库 ID
):
    """
    精确模式提取

    Args:
        file: PDF 文件
        api_key: API密钥
        model_name: 模型名称
        model_url: 模型URL
        save_file: 是否保存文件和提取结果到本地（默认 True）
        knowledge_base_id: 知识库 ID（可选）

    Returns:
        提取结果，如果 save_file=True，会保存文件和结果到本地
    """
    temp_file = None
    saved_file_path = None

    try:
        print(f"收到文件: {file.filename}")
        safe_filename = Path(file.filename).name if file.filename else "unknown.pdf"

        # 读取文件内容
        content = await file.read()
        file_size = len(content)

        # 如果需要保存文件，先保存到永久位置
        if save_file:
            # 生成文件 ID 和保存路径
            file_id = f"file_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
            date_path = datetime.now().strftime('%Y/%m/%d')
            upload_dir = UPLOAD_BASE_DIR / date_path
            upload_dir.mkdir(parents=True, exist_ok=True)

            saved_file_path = upload_dir / f"{file_id}_{safe_filename}"
            with open(saved_file_path, 'wb') as f:
                f.write(content)

            print(f"✓ 文件已保存: {saved_file_path}")
            extraction_file_path = str(saved_file_path)
        else:
            # 否则使用临时文件
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            temp_file.write(content)
            temp_file.close()
            extraction_file_path = temp_file.name

        # 执行提取
        result = await service.extract_accurate(
            file_path=extraction_file_path,
            api_key=api_key,
            model_name=model_name,
            model_url=model_url,
            original_filename=safe_filename
        )

        # 如果需要保存，保存提取结果
        if save_file:
            saved_paths = save_extraction_results(
                file_id=file_id,
                filename=safe_filename,
                result_data=result
            )
            print(f"✓ 提取结果已保存到: {saved_paths['markdown']}")

            # 在返回数据中添加保存信息
            result['saved_info'] = {
                'file_id': file_id,
                'file_path': str(saved_file_path),
                'file_size': file_size,
                'knowledge_base_id': knowledge_base_id,
                'markdown_path': saved_paths['markdown'],
                'metadata_path': saved_paths['metadata'],
                'upload_time': datetime.now().isoformat()
            }

        return ExtractionResponse(
            success=True,
            message="精确提取成功" + (" 并已保存到本地" if save_file else ""),
            filename=safe_filename,
            data=result
        )

    except Exception as e:
        print(f"精确提取失败: {e}")
        import traceback
        traceback.print_exc()

        return ExtractionResponse(
            success=False,
            message="精确提取失败",
            filename=Path(file.filename).name if file.filename else None,
            error=str(e)
        )
    finally:
        # 清理临时文件（仅在不保存文件时）
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except:
                pass


@app.get("/health")
async def health_check():
    """服务健康检查"""
    return {
        "status": "healthy",
        "service": "pdf-extraction",
        "timestamp": asyncio.get_event_loop().time()
    }


# ============ 调试模式 ============

async def debug_extract(pdf_path: str, mode: str = "fast", **kwargs):
    """
    调试模式 - 可以直接调用提取功能进行测试
    
    Args:
        pdf_path: PDF文件路径
        mode: 提取模式，"fast" 或 "accurate"
        **kwargs: 其他参数，根据模式可能需要api_key, model_name, model_url
    """
    service = PDFExtractionService()
    
    try:
        if mode == "fast":
            print(f"开始快速模式提取: {pdf_path}")
            result = await service.extract_fast(pdf_path)
            print("提取完成，结果:")
            print(f"  页数: {result['metadata']['total_pages']}")
            print(f"  图片数: {result['metadata']['total_images']}")
            print(f"  Markdown长度: {len(result['markdown'])} 字符")
            return result
            
        elif mode == "accurate":
            print(f"开始精确模式提取: {pdf_path}")
            api_key = kwargs.get('api_key', '')
            model_name = kwargs.get('model_name', '')
            model_url = kwargs.get('model_url', '')
            
            if not all([api_key, model_name, model_url]):
                raise ValueError("精确模式需要提供 api_key, model_name, model_url 参数")
                
            result = await service.extract_accurate(
                file_path=pdf_path,
                api_key=api_key,
                model_name=model_name,
                model_url=model_url
            )
            print("提取完成，结果:")
            print(f"  页数: {result['metadata']['total_pages']}")
            print(f"  表格数: {result['metadata']['total_tables']}")
            print(f"  公式数: {result['metadata']['total_formulas']}")
            print(f"  Markdown长度: {len(result['markdown'])} 字符")
            return result
            
        else:
            raise ValueError(f"不支持的模式: {momde}")
            
    except Exception as e:
        print(f"提取过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        raise


def run_debug_mode(pdf_path: str, mode: str = "fast", **kwargs):
    """运行调试模式的辅助函数"""
    import asyncio
    asyncio.run(debug_extract(pdf_path, mode, **kwargs))


# ============ 启动服务 ============

if __name__ == "__main__":
    is_debug = False  # 如果要本地运行测试，将 False 改为 True
    if is_debug:
        test_file_path  ="/home/MuyuWorkSpace/01_TrafficProject/Multimodal_RAG/backend/data/阿里开发手册-泰山版.pdf" # 这里替换成自己的

        run_debug_mode(
            test_file_path, 
            "fast",  # 或者 "accurate"
            api_key=os.getenv("API_KEY")    , 
            model_name=os.getenv("MODEL_NAME"), 
            model_url=os.getenv("MODEL_URL"))
    else:
        # 启动Web服务
        print("启动PDF提取服务...")
        uvicorn.run(
            app,
            host=SERVICE_HOST,
            port=SERVICE_PORT,
            log_level="info"
        )
