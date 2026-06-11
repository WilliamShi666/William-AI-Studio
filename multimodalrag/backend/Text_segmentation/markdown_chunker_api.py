"""
Markdown文本切分API服务
支持四种切分方式：
- V1: header_recursive（默认）, markdown_only
- V2: ocr_aware, layout_based (用于 OCR 2.0)
"""
from fastapi import FastAPI, HTTPException # type: ignore
from fastapi.middleware.cors import CORSMiddleware # type: ignore
from pydantic import BaseModel, Field # type: ignore
from typing import Optional, List, Dict, Any, Literal
import uvicorn # type: ignore
import os
import re
# 导入已有的切分模块 (V1)
from MarkdownTextSplitter import chunk_markdown_only_with_cross_page
from header_recursive import chunk_header_recursive_with_cross_page
# 导入 langchain 切分器 (V2)
try:
    from langchain_text_splitters import MarkdownTextSplitter, MarkdownHeaderTextSplitter # type: ignore
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    print("⚠️  langchain_text_splitters 未安装，V2 切分方法将不可用")


SERVICE_PORT = int(os.getenv("CHUNK_SERVICE_PORT", "8001"))
SERVICE_HOST = os.getenv("CHUNK_SERVICE_HOST", "127.0.0.1")

# ==================== V2 辅助函数（用于 OCR 2.0）====================

def extract_image_references(text: str, file_id: Optional[str] = None) -> List[Dict[str, str]]:
    """
    从markdown文本中提取图片引用

    Args:
        text: Markdown文本
        file_id: 文件ID（用于构建完整URL）

    Returns:
        图片引用列表，每个元素包含：
        - filename: 图片文件名
        - path: 相对路径（如 "images/xxx.png"）
        - url: 完整URL（如果提供了file_id）
        - alt_text: 图片alt文本
    """
    # 匹配 markdown 图片语法: ![alt](path)
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    matches = re.findall(pattern, text)

    images = []
    for alt_text, img_path in matches:
        # 只处理本地图片路径（images/xxx.png 格式）
        if 'images/' in img_path and not img_path.startswith('http'):
            filename = img_path.split('/')[-1]
            image_info = {
                "filename": filename,
                "path": img_path,
                "alt_text": alt_text
            }

            # 如果提供了file_id，构建完整URL
            if file_id:
                image_info["url"] = f"/document/{file_id}/images/{filename}"

            images.append(image_info)

    return images

def split_pages_v2(md_text: str) -> tuple:
    """按{{第N页}}分页"""
    pattern = re.compile(r"\{\{第(\d+)页\}\}")
    parts = pattern.split(md_text)
    page_blocks: List[tuple] = []
    
    prefix = parts[0]
    if prefix.strip():
        page_blocks.append((1, prefix))
    
    for i in range(1, len(parts), 2):
        page_no = int(parts[i])
        content = parts[i + 1]
        page_blocks.append((page_no, content))
    
    full_text_clean = "".join(block for _, block in page_blocks)
    return full_text_clean, page_blocks

def smart_merge_chunks_v2(chunks: List[Dict], chunk_size: int, tolerance: float, max_page_span: int) -> List[Dict]:
    """智能合并策略（V2）"""
    if not chunks:
        return []
    
    max_allowed = int(chunk_size * (1 + tolerance))
    result = []
    i = 0
    
    while i < len(chunks):
        current = chunks[i].copy()
        j = i + 1
        
        while j < len(chunks):
            next_chunk = chunks[j]
            
            if max_page_span > 0:
                potential_span = next_chunk["page_end"] - current["page_start"] + 1
                if potential_span > max_page_span:
                    break
            
            combined_len = current["text_length"] + next_chunk["text_length"]
            should_merge = False
            
            # 同页合并
            if current["page_end"] == next_chunk["page_start"]:
                if combined_len <= max_allowed:
                    should_merge = True
            # 相邻页合并
            elif next_chunk["pages"][0] - current["pages"][-1] == 1:
                if (current["text_length"] < chunk_size * 0.5 and 
                    next_chunk["text_length"] < chunk_size * 0.5 and 
                    combined_len <= max_allowed):
                    should_merge = True
                elif current["text_length"] < chunk_size * 0.3 and combined_len <= chunk_size:
                    should_merge = True
                elif (current.get("is_table_like") and next_chunk.get("is_table_like") and 
                      combined_len <= max_allowed):
                    should_merge = True
            
            if should_merge:
                current["text"] = current["text"].rstrip() + "\n" + next_chunk["text"].lstrip()
                current["text_length"] = len(current["text"])
                current["page_end"] = next_chunk["page_end"]
                current["pages"] = sorted(set(current["pages"] + next_chunk["pages"]))
                current["continued"] = True
                current["is_table_like"] = current.get("is_table_like") and next_chunk.get("is_table_like")
                if "headers" in current and "headers" in next_chunk:
                    for k, v in next_chunk["headers"].items():
                        if k not in current["headers"] or len(v) > len(current["headers"][k]):
                            current["headers"][k] = v
                j += 1
            else:
                break
        
        result.append(current)
        i = j
    
    return result

def add_cross_page_bridges_v2(chunks: List[Dict], bridge_span: int) -> List[Dict]:
    """添加跨页桥接片段（V2）"""
    if bridge_span <= 0:
        return chunks
    
    out = []
    for i, c in enumerate(chunks):
        out.append(c)
        if i + 1 < len(chunks):
            n = chunks[i + 1]
            if n["pages"][0] - c["pages"][-1] == 1:
                tail = c["text"][-bridge_span:] if len(c["text"]) >= bridge_span else c["text"]
                head = n["text"][:bridge_span] if len(n["text"]) >= bridge_span else n["text"]
                if tail.strip() and head.strip():
                    bridge_text = tail + "\n" + head
                    bridge = {
                        "page_start": c["page_end"],
                        "page_end": n["page_start"],
                        "pages": [c["page_end"], n["page_start"]],
                        "text": bridge_text,
                        "text_length": len(bridge_text),
                        "continued": True,
                        "cross_page_bridge": True,
                        "is_table_like": c.get("is_table_like") or n.get("is_table_like")
                    }
                    if "headers" in c:
                        bridge["headers"] = c["headers"].copy()
                    out.append(bridge)
    return out

def chunk_ocr_aware(md_text: str, chunk_size: int, chunk_overlap: int,
                    merge_tolerance: float, max_page_span: int, bridge_span: int, file_id: Optional[str] = None) -> Dict:
    """OCR感知切分（V2）"""
    if not LANGCHAIN_AVAILABLE:
        raise Exception("langchain_text_splitters 未安装，无法使用 V2 切分方法")
    
    full_text_clean, page_blocks = split_pages_v2(md_text)
    splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    
    raw_chunks = []
    for page_no, page_text in page_blocks:
        sub_texts = splitter.split_text(page_text)
        for sub in sub_texts:
            t = sub.strip()
            if t:
                chunk = {
                    "page_start": page_no,
                    "page_end": page_no,
                    "pages": [page_no],
                    "text": t,
                    "text_length": len(t),
                    "continued": False,
                    "cross_page_bridge": False,
                    "is_table_like": t.startswith("|") or "\n|" in t
                }
                # 提取图片引用
                images = extract_image_references(t, file_id)
                if images:
                    chunk["images"] = images
                raw_chunks.append(chunk)

    merged = smart_merge_chunks_v2(raw_chunks, chunk_size, merge_tolerance, max_page_span)
    with_bridges = add_cross_page_bridges_v2(merged, bridge_span)

    # 为每个chunk提取图片引用（合并后可能包含更多图片）
    for chunk in with_bridges:
        images = extract_image_references(chunk["text"], file_id)
        if images:
            chunk["images"] = images

    return {"full_text": full_text_clean, "chunks": with_bridges}

def chunk_layout_based(md_text: str, chunk_size: int, chunk_overlap: int,
                       merge_tolerance: float, max_page_span: int, bridge_span: int, file_id: Optional[str] = None) -> Dict:
    """版面感知切分（V2）"""
    if not LANGCHAIN_AVAILABLE:
        raise Exception("langchain_text_splitters 未安装，无法使用 V2 切分方法")
    
    full_text_clean, page_blocks = split_pages_v2(md_text)
    
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
        ("####", "Header 4"),
    ]
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=False
    )
    recursive_splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    
    raw_chunks = []
    for page_no, page_text in page_blocks:
        header_splits = header_splitter.split_text(page_text)
        
        for hd_doc in header_splits:
            hd_meta = getattr(hd_doc, 'metadata', {})
            hd_text = hd_doc if isinstance(hd_doc, str) else getattr(hd_doc, 'page_content', str(hd_doc))
            
            sub_texts = recursive_splitter.split_text(hd_text)
            
            for sub in sub_texts:
                t = sub.strip()
                if t:
                    chunk = {
                        "page_start": page_no,
                        "page_end": page_no,
                        "pages": [page_no],
                        "text": t,
                        "text_length": len(t),
                        "continued": False,
                        "cross_page_bridge": False,
                        "is_table_like": t.startswith("|") or "\n|" in t
                    }
                    if hd_meta:
                        chunk["headers"] = hd_meta
                    # 提取图片引用
                    images = extract_image_references(t, file_id)
                    if images:
                        chunk["images"] = images
                    raw_chunks.append(chunk)

    merged = smart_merge_chunks_v2(raw_chunks, chunk_size, merge_tolerance, max_page_span)
    with_bridges = add_cross_page_bridges_v2(merged, bridge_span)

    # 为每个chunk提取图片引用（合并后可能包含更多图片）
    for chunk in with_bridges:
        images = extract_image_references(chunk["text"], file_id)
        if images:
            chunk["images"] = images

    return {"full_text": full_text_clean, "chunks": with_bridges}

# ==================== FastAPI App ====================

app = FastAPI(
    title="Markdown文本切分API",
    description="提供 V1(header_recursive/markdown_only) 和 V2(ocr_aware/layout_based) 切分策略",
    version="2.0.0"
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 数据模型 ====================

class ChunkingConfig(BaseModel):
    """切分配置"""
    method: Literal["header_recursive", "markdown_only"] = Field(
        default="header_recursive",
        description="切分方法：header_recursive(标题+递归) 或 markdown_only(纯Markdown)"
    )
    chunk_size: int = Field(default=1500, ge=100, le=10000, description="目标chunk大小")
    chunk_overlap: int = Field(default=200, ge=0, le=1000, description="chunk重叠长度")
    merge_tolerance: float = Field(default=0.2, ge=0, le=1.0, description="合并容忍度")
    max_page_span: int = Field(default=3, ge=0, le=10, description="最大跨页数，0表示不限制")
    bridge_span: int = Field(default=150, ge=0, le=500, description="跨页桥接片段长度")
    add_bridges: bool = Field(default=True, description="是否添加跨页桥接片段")

class ChunkResult(BaseModel):
    """单个切分块"""
    page_start: int
    page_end: int
    pages: List[int]
    text: str
    text_length: int
    headers: Optional[Dict[str, str]] = None
    continued: bool
    cross_page_bridge: bool
    is_table_like: bool

class ChunkingResponse(BaseModel):
    """切分结果响应"""
    success: bool
    message: str
    filename: Optional[str] = None
    data: Dict[str, Any]
    error: Optional[str] = None

class ChunkingRequest(BaseModel):
    """切分请求"""
    markdown: str = Field(..., description="Markdown文本内容")
    filename: Optional[str] = Field(None, description="文件名（可选）")
    config: ChunkingConfig = Field(default_factory=ChunkingConfig, description="切分配置")
    metadata: Optional[Dict[str, Any]] = Field(None, description="原始元数据（可选）")

# ==================== API端点 ====================

@app.get("/")
async def root():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "Markdown文本切分API",
        "version": "2.0.0",
        "methods": {
            "v1": ["header_recursive", "markdown_only"],
            "v2": ["ocr_aware", "layout_based"]
        },
        "endpoints": {
            "v1": "/chunk",
            "v2": "/chunk/v2"
        }
    }

@app.post("/chunk", response_model=ChunkingResponse)
async def chunk_markdown(request: ChunkingRequest):
    """
    对Markdown文本进行切分
    
    - **markdown**: Markdown文本内容（必填）
    - **config**: 切分配置（可选，使用默认值）
    - **filename**: 文件名（可选）
    - **metadata**: 原始元数据（可选）
    
    返回在原数据基础上添加chunks字段的结果
    """
    try:
        # 根据配置选择切分方法
        if request.config.method == "header_recursive":
            # 调用 header_recursive 模块
            result = chunk_header_recursive_with_cross_page(
                md_text=request.markdown,
                chunk_size=request.config.chunk_size,
                chunk_overlap=request.config.chunk_overlap,
                merge_tolerance=request.config.merge_tolerance,
                max_page_span=request.config.max_page_span,
                bridge_span=request.config.bridge_span if request.config.add_bridges else 0
            )
        else:  # markdown_only
            # 调用 MarkdownTextSplitter 模块
            result = chunk_markdown_only_with_cross_page(
                md_text=request.markdown,
                chunk_size=request.config.chunk_size,
                chunk_overlap=request.config.chunk_overlap,
                merge_tolerance=request.config.merge_tolerance,
                max_page_span=request.config.max_page_span,
                bridge_span=request.config.bridge_span if request.config.add_bridges else 0
            )
        
        # 构建返回数据 - 保留原有的metadata，并添加新字段
        response_data = {}
        
        # 如果有原始元数据，先保留它
        if request.metadata:
            response_data.update(request.metadata)
        
        # 添加或更新字段
        response_data.update({
            "markdown": request.markdown,
            "full_text": result["full_text"],
            "chunks": result["chunks"],
            "chunking_config": request.config.dict(),
            "chunk_stats": {
                "total_chunks": len(result["chunks"]),
                "bridge_chunks": sum(1 for c in result["chunks"] if c["cross_page_bridge"]),
                "cross_page_chunks": sum(1 for c in result["chunks"] if c["continued"] and not c["cross_page_bridge"]),
                "single_page_chunks": sum(1 for c in result["chunks"] if not c["continued"] and not c["cross_page_bridge"]),
                "table_chunks": sum(1 for c in result["chunks"] if c["is_table_like"]),
                "avg_chunk_length": sum(c["text_length"] for c in result["chunks"]) / len(result["chunks"]) if result["chunks"] else 0
            }
        })
        
        return ChunkingResponse(
            success=True,
            message=f"切分成功，使用方法: {request.config.method}",
            filename=request.filename,
            data=response_data,
            error=None
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"切分失败: {str(e)}"
        )

@app.post("/chunk/from-result", response_model=ChunkingResponse)
async def chunk_from_result(
    result: Dict[str, Any],
    config: ChunkingConfig = ChunkingConfig()
):
    """
    从accurate_result.json格式的数据进行切分
    
    接受完整的accurate_result.json格式，在data字段中添加chunks相关信息
    """
    try:
        # 提取markdown内容
        if not result.get("success"):
            raise HTTPException(status_code=400, detail="输入数据标记为失败")
        
        data = result.get("data", {})
        markdown = data.get("markdown")
        
        if not markdown:
            raise HTTPException(status_code=400, detail="未找到markdown字段")
        
        # 执行切分
        if config.method == "header_recursive":
            # 调用 header_recursive 模块
            chunk_result = chunk_header_recursive_with_cross_page(
                md_text=markdown,
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
                merge_tolerance=config.merge_tolerance,
                max_page_span=config.max_page_span,
                bridge_span=config.bridge_span if config.add_bridges else 0
            )
        else:  # markdown_only
            # 调用 MarkdownTextSplitter 模块
            chunk_result = chunk_markdown_only_with_cross_page(
                md_text=markdown,
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
                merge_tolerance=config.merge_tolerance,
                max_page_span=config.max_page_span,
                bridge_span=config.bridge_span if config.add_bridges else 0
            )
        
        # 在原data基础上添加chunks信息（保留所有原有字段如images等）
        result["data"]["chunks"] = chunk_result["chunks"]
        result["data"]["full_text"] = chunk_result["full_text"]
        result["data"]["chunking_config"] = config.dict()
        result["data"]["chunk_stats"] = {
            "total_chunks": len(chunk_result["chunks"]),
            "bridge_chunks": sum(1 for c in chunk_result["chunks"] if c["cross_page_bridge"]),
            "cross_page_chunks": sum(1 for c in chunk_result["chunks"] if c["continued"] and not c["cross_page_bridge"]),
            "single_page_chunks": sum(1 for c in chunk_result["chunks"] if not c["continued"] and not c["cross_page_bridge"]),
            "table_chunks": sum(1 for c in chunk_result["chunks"] if c["is_table_like"]),
            "avg_chunk_length": sum(c["text_length"] for c in chunk_result["chunks"]) / len(chunk_result["chunks"]) if chunk_result["chunks"] else 0
        }
        
        # 更新message
        result["message"] = f"{result.get('message', '')} - 切分完成({config.method})"
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"切分失败: {str(e)}"
        )

@app.post("/chunk/v2")
async def chunk_from_output_json(request: Dict[str, Any]):
    """
    从 output.json 格式进行切分（OCR 2.0 专用）
    
    请求格式：
    {
    "backend": "pipeline",
    "version": "2.5.4",
    "results": {
        "文件名": {
        "md_content": "# 标题\n内容...",           // Markdown文本
        
        "middle_json": {                          // 布局中间数据
            "pdf_info": [{
            "preproc_blocks": [                   // 预处理的布局块
                {
                "type": "title",                  // 块类型：title/text/image/table
                "bbox": [163, 174, 499, 251],     // 坐标位置
                "lines": [...],                   // 行信息
                "index": 0
                }
            ]
            }]
        },
        
        "model_output": [                         // 模型原始输出
            {
            "layout_dets": [                      // 布局检测结果
                {
                "category_id": 1,                 // 类别ID (0:title, 1:text, 3:image, 5:table)
                "poly": [...],                    // 多边形坐标
                "bbox": [...],                    // 边界框
                "score": 0.95                     // 置信度
                }
            ],
            "page_info": {                        // 页面信息
                "width": 595,
                "height": 841
            }
            }
        ],
        
        "content_list": [                         // 结构化内容列表
            {
            "type": "text",                       // 内容类型
            "text": "Java开发手册",
            "text_level": 1,                      // 文本层级
            "bbox": [273, 206, 838, 298],         // 位置信息
            "page_idx": 0                         // 页码
            }
        ],
        
        "images": {                               // 裁切的图片（base64）
            "images/xxx.png": "iVBORw0KGgo..."
        },
        
        "page_images": ["base64...", "..."],     //完整页面图片
        
        "chunks": [...]                           // 切分后的块
        }
    }
    }
    
    返回：在原 output.json 基础上添加 chunks 字段
    """
    try:
     
        output_json = request.get("output_json", {})
        result_key = request.get("result_key")
        config_dict = request.get("config", {})
        file_id = request.get("file_id")  # 提取file_id用于构建图片URL

        # 提取配置
        method = config_dict.get("method", "ocr_aware")
        chunk_size = config_dict.get("chunk_size", 1500)
        chunk_overlap = config_dict.get("chunk_overlap", 200)
        merge_tolerance = config_dict.get("merge_tolerance", 0.2)
        max_page_span = config_dict.get("max_page_span", 3)
        bridge_span = config_dict.get("bridge_span", 150)
        add_bridges = config_dict.get("add_bridges", True)
        

        # 提取 markdown 内容
        results = output_json.get("results", {})
        if not results:
            raise HTTPException(status_code=400, detail="output_json 中未找到 results 字段")
        
        # 如果没有指定 key，使用第一个 key
        if not result_key or result_key not in results:
            if results:
                result_key = list(results.keys())[0]
                print(f"ℹ未指定 result_key，使用第一个键: {result_key}")
            else:
                raise HTTPException(status_code=400, detail="results 为空")
        
        result_data = results[result_key]
        
        # 打印输入数据信息
        if 'images' in result_data:
            print(f"  - Images 数量: {len(result_data['images'])}")
        if 'page_images' in result_data:
            print(f"  - Page images 数量: {len(result_data['page_images'])}")
        
        markdown = result_data.get("md_content")
        
        if not markdown:
            raise HTTPException(status_code=400, detail=f"未找到 md_content 字段")

   
        # 执行切
        if method == "ocr_aware":
            # 先分页，再在每页内切分
            # 智能合并：考虑页面边界，避免不合理的跨页
            # 表格保护：识别表格并尽量保持完整
            chunk_result = chunk_ocr_aware(
                md_text=markdown,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                merge_tolerance=merge_tolerance,
                max_page_span=max_page_span,
                bridge_span=bridge_span if add_bridges else 0,
                file_id=file_id
            )
        elif method == "layout_based":
            # 标题层级感知：基于 Markdown 标题结构切分
            # 保留上下文：每个 chunk 包含标题元数据
            # 页面 + 布局双重感知
            chunk_result = chunk_layout_based(
                md_text=markdown,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                merge_tolerance=merge_tolerance,
                max_page_span=max_page_span,
                bridge_span=bridge_span if add_bridges else 0,
                file_id=file_id
            )
        else:
            raise HTTPException(status_code=400, detail=f"不支持的切分方法: {method}")
        
        # 在原始数据上添加切分结果（保留所有字段：images、page_images 等）
        chunks = chunk_result["chunks"]
        chunk_stats = {
            "total_chunks": len(chunks),
            "bridge_chunks": sum(1 for c in chunks if c.get("cross_page_bridge")),
            "cross_page_chunks": sum(1 for c in chunks if c.get("continued") and not c.get("cross_page_bridge")),
            "single_page_chunks": sum(1 for c in chunks if not c.get("continued") and not c.get("cross_page_bridge")),
            "table_chunks": sum(1 for c in chunks if c.get("is_table_like")),
            "avg_chunk_length": sum(c["text_length"] for c in chunks) / len(chunks) if chunks else 0
        }
        
        output_json["results"][result_key]["chunks"] = chunks
        output_json["results"][result_key]["full_text"] = chunk_result["full_text"]
        output_json["results"][result_key]["chunking_config"] = config_dict
        output_json["results"][result_key]["chunk_stats"] = chunk_stats
        
        print("\n" + "="*80)
        print("✅ OCR 2.0 切分完成")
        print("="*80)
        print(f"📊 切分统计:")
        print(f"  - 总 chunks: {chunk_stats['total_chunks']}")
        print(f"  - 跨页桥接 chunks: {chunk_stats['bridge_chunks']}")
        print(f"  - 跨页普通 chunks: {chunk_stats['cross_page_chunks']}")
        print(f"  - 单页 chunks: {chunk_stats['single_page_chunks']}")
        print(f"  - 表格 chunks: {chunk_stats['table_chunks']}")
        print(f"  - 平均长度: {chunk_stats['avg_chunk_length']:.0f} 字符")
        
        # 打印前3个 chunks 的详细信息
        print(f"\n📝 前 3 个 chunks 详情:")
        for i, chunk in enumerate(chunks[:3]):
            print(f"  Chunk {i+1}:")
            print(f"    - 页码: {chunk['page_start']}-{chunk['page_end']} (涉及页: {chunk['pages']})")
            print(f"    - 长度: {chunk['text_length']} 字符")
            print(f"    - 跨页: {chunk.get('continued', False)}")
            print(f"    - 桥接: {chunk.get('cross_page_bridge', False)}")
            print(f"    - 表格: {chunk.get('is_table_like', False)}")
            if 'headers' in chunk:
                print(f"    - 标题: {chunk['headers']}")
            print(f"    - 文本预览: {chunk['text'][:80]}...")
        
        print("\n" + "="*80)
        print(f"✅ 返回数据包含: {list(output_json['results'][result_key].keys())}")
        print("="*80 + "\n")
        
        return output_json
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"切分失败: {str(e)}")

# ==================== 启动配置 ====================

if __name__ == "__main__":
    uvicorn.run(
        "markdown_chunker_api:app",
        host=SERVICE_HOST,
        port=SERVICE_PORT,
        reload=True,
        log_level="info"
    )
