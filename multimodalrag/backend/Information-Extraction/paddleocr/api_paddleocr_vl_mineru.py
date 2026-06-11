#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PaddleOCR-VL API Service
纯净的 API 服务，输入 PDF/图片，输出 JSON 格式的解析结果
"""

import os
import io
import re
import json
import base64
from pathlib import Path
from typing import Dict, List, Any
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

try:
    import html2text
except ImportError:
    raise ImportError("请安装 html2text: pip install html2text")

try:
    from paddleocr import PaddleOCRVL
except ImportError:
    raise ImportError("请安装 paddleocr: pip install paddleocr")

# ============ 配置 ============
VL_MODEL_DIR = os.getenv("PADDLEOCR_VL_MODEL_DIR", "./models/PaddleOCR-VL-0.9B")
LAYOUT_MODEL_DIR = os.getenv("PADDLEOCR_LAYOUT_MODEL_DIR", "./models/PP-DocLayoutV2")
GPU_ID = os.getenv("PADDLEOCR_GPU_ID", "0")
PORT = int(os.getenv("PADDLEOCR_PORT", "8802"))
HOST = os.getenv("PADDLEOCR_HOST", "127.0.0.1")
# ===============================

os.environ["CUDA_VISIBLE_DEVICES"] = GPU_ID

app = FastAPI(title="PaddleOCR-VL API", version="1.0.0")

# 全局 pipeline 实例
pipeline = None


def pil_to_base64(img, fmt="PNG") -> str:
    """PIL Image 转 base64"""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def convert_html_to_markdown(md_text: str) -> str:
    """将 HTML 格式的图片和表格转换为 Markdown 格式"""
    # 转换图片
    img_pattern = r'<div[^>]*>\s*<img\s+src="([^"]+)"[^>]*>\s*</div>'
    md_text = re.sub(img_pattern, r'![](\1)', md_text)
    
    # 转换表格
    if '<table' in md_text:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        
        table_pattern = r'<table[^>]*>.*?</table>'
        tables = re.findall(table_pattern, md_text, re.DOTALL)
        
        for table_html in tables:
            table_md = h.handle(table_html).strip()
            md_text = md_text.replace(table_html, table_md)
    
    return md_text


def process_file(file_path: str) -> Dict[str, Any]:
    """处理文件并返回 JSON 结果"""
    outputs = pipeline.predict(input=file_path)
    
    model_output_list = []
    content_list = []
    images_dict = {}
    page_images_list = []
    
    for page_idx, res in enumerate(outputs):
        md_dict = res.markdown
        md_text_original = md_dict["markdown_texts"]
        md_images = md_dict["markdown_images"]
        
        # 收集原始 markdown
        model_output_list.append(md_text_original)
        
        # 转换图片为 base64
        for rel_path, pil_img in md_images.items():
            images_dict[rel_path] = pil_to_base64(pil_img)
        
        # 提取 blocks
        parsing_list = res["parsing_res_list"]
        for bi, blk in enumerate(parsing_list):
            label = blk.label
            text = blk.content
            bbox = blk.bbox
            
            content_list.append({
                "page_idx": page_idx,
                "block_idx": bi,
                "type": label,
                "text": text,
                "bbox": bbox
            })
        
        # 提取页面可视化图
        layout_det = res.img["layout_det_res"]
        page_images_list.append(pil_to_base64(layout_det))
    
    # 合并并转换 markdown
    combined_md = "\n\n".join(model_output_list)
    md_content_converted = convert_html_to_markdown(combined_md)
    
    # 组装最终输出
    return {
        "backend": "paddleocr-vl",
        "version": "0.9B",
        "results": {
            Path(file_path).stem: {
                "md_content": md_content_converted,
                "model_output": model_output_list,
                "middle_json": {},
                "content_list": content_list,
                "images": images_dict,
                "page_images": page_images_list
            }
        }
    }


@app.on_event("startup")
async def startup_event():
    """启动时初始化 pipeline"""
    global pipeline
    
    vl_dir = Path(VL_MODEL_DIR)
    layout_dir = Path(LAYOUT_MODEL_DIR)
    
    if not vl_dir.exists():
        raise FileNotFoundError(f"VL model not found: {vl_dir}")
    if not layout_dir.exists():
        raise FileNotFoundError(f"Layout model not found: {layout_dir}")
    
    pipeline = PaddleOCRVL(
        vl_rec_model_dir=str(vl_dir),
        layout_detection_model_dir=str(layout_dir)
    )
    print(f"[OK] Pipeline initialized on GPU {GPU_ID}")


@app.post("/parse")
async def parse_document(file: UploadFile = File(...)):
    """
    解析文档（PDF 或图片）
    
    Returns:
        JSON 格式的解析结果
    """
    # 保存上传的文件
    temp_path = f"/tmp/{file.filename}"
    with open(temp_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    try:
        result = process_file(temp_path)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 清理临时文件
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "gpu": GPU_ID}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
