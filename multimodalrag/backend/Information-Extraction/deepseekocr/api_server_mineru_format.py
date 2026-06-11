#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek OCR API Server (MinerU-compatible output)
输出格式对齐 MinerU 的 model_output / middle_json / content_list
修复点：
- 兼容 xyxy / xywh / 归一化坐标
- 画布(base_size×base_size) → 原图坐标映射（去补边 + 反缩放）
- 裁切安全边距，避免下边截断与“显得窄”
"""

import os
import io
import re
import argparse
import base64
import tempfile
import shutil
import uuid
from io import BytesIO
from typing import List, Optional, Dict, Tuple

import torch
from PIL import Image, ImageDraw, ImageFont

# PDF -> images
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from transformers import AutoTokenizer, AutoModel


# -----------------------
# FastAPI App & CORS
# -----------------------
app = FastAPI(title="DeepSeek OCR API Service", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# -----------------------
# Globals
# -----------------------
model = None
tokenizer = None
device = None


# -----------------------
# Utils
# -----------------------
def _supports_bf16() -> bool:
    try:
        return torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    except Exception:
        return False


def image_to_base64(image: Image.Image) -> str:
    """PIL Image -> base64 (PNG)"""
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    img_bytes = buffered.getvalue()
    return base64.b64encode(img_bytes).decode('utf-8')


def generate_image_filename() -> str:
    """生成随机图片文件名（与 PNG 保存一致）"""
    return f"images/{uuid.uuid4().hex[:32]}.png"


def bbox_to_poly(bbox: List[float]) -> List[float]:
    """bbox(x0,y0,x1,y1) -> poly 顺时针四边形"""
    x0, y0, x1, y1 = bbox
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def parse_bbox4(
    bbox4: List[float],
    base_canvas_size: Optional[int] = None
) -> Tuple[List[float], str]:
    """
    ✅ 修正: DeepSeek-OCR 输出的是 0-999 归一化坐标,直接映射到原图
    """
    if len(bbox4) != 4:
        return [0., 0., 0., 0.], "xyxy"

    x0, y0, a, b = map(float, bbox4)

    # ✅ DeepSeek-OCR 输出范围是 0-999
    DEEPSEEK_NORM_RANGE = 999.0
    
    # 判断是否是归一化坐标 (0-999)
    is_deepseek_normalized = (0.0 <= min(x0, y0, a, b) <= DEEPSEEK_NORM_RANGE) and \
                             (max(x0, y0, a, b) <= DEEPSEEK_NORM_RANGE)
    
    # ✅ xywh / xyxy 判断
    is_xywh = (a <= x0) or (b <= y0)
    
    if is_xywh:
        x1 = x0 + max(a, 0.0)
        y1 = y0 + max(b, 0.0)
        mode = "xywh"
    else:
        x1, y1 = a, b
        mode = "xyxy"
    
    # 容错排序
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    
    # ✅ 返回归一化的 xyxy (0-999)
    return [x0, y0, x1, y1], mode


def map_canvas_bbox_to_image(
    bbox_canvas_xyxy: List[float],
    img_w: int,
    img_h: int,
    base_canvas_size: int = 999,  # ✅ DeepSeek-OCR 的归一化范围
) -> List[float]:
    """
    ✅ 修正: DeepSeek-OCR 输出的是 0-999 归一化坐标
    直接映射到原图,不需要考虑 padding
    """
    x0, y0, x1, y1 = bbox_canvas_xyxy
    
    # ✅ 从 0-999 映射到原图坐标
    x0_ = (x0 / 999.0) * img_w
    y0_ = (y0 / 999.0) * img_h
    x1_ = (x1 / 999.0) * img_w
    y1_ = (y1 / 999.0) * img_h
    
    # ✅ Clamp 到原图范围
    x0_ = max(0.0, min(float(img_w), x0_))
    y0_ = max(0.0, min(float(img_h), y0_))
    x1_ = max(0.0, min(float(img_w), x1_))
    y1_ = max(0.0, min(float(img_h), y1_))
    
    # ✅ 保证顺序
    if x1_ < x0_:
        x0_, x1_ = x1_, x0_
    if y1_ < y0_:
        y0_, y1_ = y1_, y0_
    
    return [x0_, y0_, x1_, y1_]


def add_margin_xyxy(bbox_xyxy: List[float], img_w: int, img_h: int, ratio: float = 0.02) -> List[float]:
    """
    在 xyxy 框基础上加相对边距，默认 2%。
    可显著减轻“底部截断/看起来偏窄”的主观问题。
    """
    x0, y0, x1, y1 = bbox_xyxy
    dx = (x1 - x0) * ratio
    dy = (y1 - y0) * ratio
    x0 -= dx; y0 -= dy; x1 += dx; y1 += dy

    x0 = max(0.0, min(float(img_w), x0))
    y0 = max(0.0, min(float(img_h), y0))
    x1 = max(0.0, min(float(img_w), x1))
    y1 = max(0.0, min(float(img_h), y1))

    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    return [x0, y0, x1, y1]


def crop_image_region(image: Image.Image, bbox_xyxy: List[float]) -> Image.Image:
    """按照 xyxy 像素坐标裁切（此处才做 int 量化）"""
    x0, y0, x1, y1 = bbox_xyxy
    x0_i, y0_i, x1_i, y1_i = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))

    width, height = image.size
    x0_i = max(0, min(x0_i, width))
    y0_i = max(0, min(y0_i, height))
    x1_i = max(x0_i, min(x1_i, width))
    y1_i = max(y0_i, min(y1_i, height))

    if x1_i <= x0_i or y1_i <= y0_i:
        return Image.new("RGB", (1, 1), (255, 255, 255))

    return image.crop((x0_i, y0_i, x1_i, y1_i))


def describe_image_region(cropped_image: Image.Image, prompt: Optional[str] = None) -> str:
    """选用模型生成图片描述（可选）- 已禁用"""
    return ""  # 图片描述功能已禁用
    # 以下为原代码，保留备用
    # if model is None or tokenizer is None:
    #     return ""
    # if not prompt:
    #     prompt = "<image>\n详细描述这张图片的内容，包括主要元素、颜色、布局等。"
    #
    # img_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    # img_tmp_path = img_tmp.name
    # img_tmp.close()
    # cropped_image.save(img_tmp_path, format="PNG")
    # out_dir = tempfile.mkdtemp(prefix="dpsk_img_desc_")
    #
    # try:
    #     if not hasattr(model, "infer"):
    #         return ""
    #     description = model.infer(
    #         tokenizer=tokenizer,
    #         prompt=prompt,
    #         image_file=img_tmp_path,
    #         output_path=out_dir,
    #         base_size=512,
    #         image_size=512,
    #         crop_mode=False,
    #         save_results=False,
    #         test_compress=False,
    #         eval_mode=True,
    #     )
    #     if description:
    #         description = re.sub(r'<\|.*?\|>', '', str(description)).strip()
    #     return description or ""
    # except Exception as e:
    #     print(f"⚠️ 图片描述生成失败: {e}")
    #     return ""
    # finally:
    #     try: os.remove(img_tmp_path)
    #     except: pass
    #     try: shutil.rmtree(out_dir, ignore_errors=True)
    #     except: pass


def pdf_to_images(pdf_bytes: bytes, dpi: int = 144) -> List[Image.Image]:
    """将 PDF 字节流转为 PIL Images（渲染到指定 dpi）"""
    if fitz is None:
        raise RuntimeError(
            "PDF input requires optional PyMuPDF dependencies. Install "
            "requirements-pdf-agpl.txt only if your use case complies with "
            "AGPL obligations or you have an Artifex commercial license."
        )

    images: List[Image.Image] = []
    pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in range(pdf_document.page_count):
        page = pdf_document[page_num]
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        Image.MAX_IMAGE_PIXELS = None

        img_data = pixmap.tobytes("png")
        img = Image.open(io.BytesIO(img_data))

        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        images.append(img)

    pdf_document.close()
    return images


# -----------------------
# DeepSeek 输出解析 → MinerU 三套结构
# -----------------------
# 定义颜色映射表
BLOCK_TYPE_COLORS = {
    "title": "#FF6B6B",           # 红色 - 标题
    "sub_title": "#FF6B6B",       # 红色 - 副标题 (与 title 相同)
    "text": "#4ECDC4",            # 青色 - 文本
    "abandon": "#95A5A6",         # 灰色 - 废弃内容
    "image": "#9B59B6",           # 紫色 - 图片
    "image_caption": "#E74C3C",   # 深红 - 图片标题
    "table": "#F39C12",           # 橙色 - 表格
    "table_caption": "#E67E22",   # 深橙 - 表格标题
    "table_footnote": "#D35400",  # 更深橙 - 表格脚注
    "isolate_formula": "#3498DB", # 蓝色 - 独立公式
    "formula_caption": "#2980B9", # 深蓝 - 公式标签
    "inline_formula": "#1ABC9C",  # 绿松石 - 行内公式
    "ocr_text": "#2ECC71",        # 绿色 - OCR文本
}


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """十六进制颜色转 RGB"""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def parse_deepseek_output_to_mineru(
    text: str,
    page_idx: int,
    source_image: Image.Image,
    image_desc_prompt: Optional[str],
    enable_image_description: bool,
    base_canvas_size: Optional[int],
    draw_bbox: bool = False,
) -> Tuple[dict, dict, List[dict], Dict[str, str], Optional[str]]:
    """
    把 DeepSeek 的 raw text 转为 MinerU 的：
    - model_output（这里我们额外返回 layout_dets_page）
    - middle_json_page
    - content_list_items
    并返回裁切出的图片字典 images_base64 和带框的页面图片 page_image_base64
    """
    img_width, img_height = source_image.size

    model_output_page = {
        "layout_dets": [],
        "page_info": {"page_no": page_idx, "width": img_width, "height": img_height}
    }
    middle_json_page = {"page_idx": page_idx, "page_size": [img_width, img_height], "preproc_blocks": []}
    content_list_items: List[dict] = []
    images_base64: Dict[str, str] = {}
    
    # ✅ 用于收集所有 bbox (如果需要画框)
    all_bboxes = [] if draw_bbox else None

    # 1) 清理统计信息
    cleaned_text = re.sub(r'={50,}.*?={50,}', '', text, flags=re.DOTALL)
    # 2) 按 ref 块分段
    segments = re.split(r'(<\|ref\|>.*?<\|/ref\|>)', cleaned_text)

    i = 0
    while i < len(segments):
        segment = segments[i].strip()
        if not segment:
            i += 1
            continue

        ref_match = re.match(r'<\|ref\|>(.*?)<\|/ref\|>', segment)
        if not ref_match:
            i += 1
            continue

        block_type = ref_match.group(1).strip()

        if i + 1 < len(segments):
            next_segment = segments[i + 1]
            det_match = re.match(r'<\|det\|>\[\[(.*?)\]\]<\|/det\|>\s*(.*)', next_segment, re.DOTALL)
            if not det_match:
                i += 1
                continue

            bbox_str = det_match.group(1).strip()
            content = det_match.group(2).strip()

            # 3) 解析四元组 → 画布 xyxy
            try:
                raw = [float(x.strip()) for x in bbox_str.split(',')]
            except:
                raw = [0., 0., 0., 0.]
            bbox_canvas_xyxy, mode = parse_bbox4(raw, base_canvas_size=base_canvas_size)

            # 4) 画布 → 原图
            if base_canvas_size:
                mapped_bbox = map_canvas_bbox_to_image(bbox_canvas_xyxy, img_width, img_height, base_canvas_size)
            else:
                mapped_bbox = bbox_canvas_xyxy

            # 5) 安全边距
            mapped_bbox = add_margin_xyxy(mapped_bbox, img_width, img_height, ratio=0.0)

            # poly / poly(canvas)
            poly_canvas = bbox_to_poly(bbox_canvas_xyxy)

            # 6) 构建三套结构
            layout_det = {"type": block_type, "poly": poly_canvas, "score": 1.0}
            preproc_block = {"type": block_type, "bbox": bbox_canvas_xyxy, "poly": poly_canvas}
            content_item = {"type": block_type, "bbox": bbox_canvas_xyxy, "page_idx": page_idx}

            if block_type == "table":
                html_match = re.search(r'<table>.*?</table>', content, re.DOTALL)
                if html_match:
                    html_content = html_match.group(0)
                    layout_det["html"] = html_content
                    preproc_block["html"] = html_content
                    content_item["table_body"] = html_content
                    content_item["table_caption"] = []
                    content_item["table_footnote"] = []

                    text_content = re.sub(r'<[^>]+>', ' ', html_content)
                    text_content = re.sub(r'\s+', ' ', text_content).strip()
                    preproc_block["text"] = text_content
                else:
                    preproc_block["text"] = content
                    content_item["text"] = content

            elif block_type == "image":
                img_filename = ""
                img_description = ""

                cropped = crop_image_region(source_image, mapped_bbox)
                img_base64 = image_to_base64(cropped)
                img_filename = generate_image_filename()
                images_base64[img_filename] = img_base64

                if enable_image_description:
                    print(f"🖼️  正在处理图片: {img_filename}")
                    img_description = describe_image_region(cropped, image_desc_prompt)

                layout_det["image_path"] = img_filename
                preproc_block["image_path"] = img_filename
                content_item["img_path"] = img_filename
                content_item["image_caption"] = [img_description] if img_description else []
                content_item["image_footnote"] = []
                if img_description:
                    preproc_block["description"] = img_description

            elif block_type in ("isolate_formula", "inline_formula", "formula"):
                layout_det["latex"] = content
                preproc_block["latex"] = content
                content_item["text"] = content

            elif block_type in ("title", "sub_title"):
                layout_det["text"] = content
                preproc_block["text"] = content
                content_item["text"] = content
                content_item["text_level"] = 1 if block_type == "title" else 2

            else:
                layout_det["text"] = content
                preproc_block["text"] = content
                content_item["text"] = content

            model_output_page["layout_dets"].append(layout_det)
            middle_json_page["preproc_blocks"].append(preproc_block)
            if (content_item.get("text") or content_item.get("table_body") or content_item.get("img_path")):
                content_list_items.append(content_item)

            # ✅ 收集 bbox (包含类型信息)
            if draw_bbox:
                all_bboxes.append((block_type, mapped_bbox))

            i += 2
            continue

        i += 1

    # ✅ 画框并转 base64 (带颜色和标签)
    page_image_base64 = None
    if draw_bbox and all_bboxes:
        img_with_boxes = source_image.copy()
        draw = ImageDraw.Draw(img_with_boxes)
        
        # ✅ 尝试加载字体 (如果失败则使用默认字体)
        try:
            # 优先使用支持中文的字体
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        except:
            try:
                font = ImageFont.truetype("arial.ttf", 16)
            except:
                font = ImageFont.load_default()
        
        for block_type, bbox in all_bboxes:
            x0, y0, x1, y1 = bbox
            
            # ✅ 获取颜色 (默认红色)
            color_hex = BLOCK_TYPE_COLORS.get(block_type, "#FF0000")
            color_rgb = hex_to_rgb(color_hex)
            
            # ✅ 绘制边框 (加粗)
            draw.rectangle([x0, y0, x1, y1], outline=color_rgb, width=3)
            
            # ✅ 绘制标签背景 (半透明)
            label_text = block_type
            
            # 获取文字边界框
            try:
                bbox_text = draw.textbbox((x0, y0), label_text, font=font)
                text_width = bbox_text[2] - bbox_text[0]
                text_height = bbox_text[3] - bbox_text[1]
            except:
                # 如果 textbbox 不支持 (旧版 Pillow),使用 textsize
                try:
                    text_width, text_height = draw.textsize(label_text, font=font)
                except:
                    text_width, text_height = len(label_text) * 10, 20
            
            # 标签位置 (框的左上角外侧)
            label_x = x0
            label_y = max(0, y0 - text_height - 4)
            
            # 绘制标签背景
            draw.rectangle(
                [label_x, label_y, label_x + text_width + 8, label_y + text_height + 4],
                fill=color_rgb
            )
            
            # 绘制标签文字 (白色)
            draw.text(
                (label_x + 4, label_y + 2),
                label_text,
                fill=(255, 255, 255),
                font=font
            )
        
        page_image_base64 = image_to_base64(img_with_boxes)

    return model_output_page, middle_json_page, content_list_items, images_base64, page_image_base64


def build_markdown_from_content_list(content_list: List[dict], images_base64: Dict[str, str]) -> str:
    """从 content_list 构建 Markdown"""
    md_lines = []
    for item in content_list:
        item_type = item.get("type", "text")

        if item_type == "table":
            table_body = item.get("table_body", "")
            if table_body:
                md_lines.append(table_body)
                md_lines.append("")

        elif item_type == "image":
            img_path = item.get("img_path", "")
            captions = item.get("image_caption", [])
            if img_path:
                md_lines.append(f"![]({img_path})")
                for caption in captions:
                    if caption:
                        md_lines.append(caption)
                md_lines.append("")

        elif item_type in ("isolate_formula", "inline_formula", "formula"):
            text = item.get("text", "")
            if item_type == "isolate_formula":
                md_lines.append(f"$${text}$$")
            else:
                md_lines.append(f"${text}$")
            md_lines.append("")

        elif item_type in ("title", "sub_title"):
            text = item.get("text", "")
            level = item.get("text_level", 1)
            md_lines.append(f"{'#' * level} {text}")
            md_lines.append("")

        elif item_type in ("table_caption", "image_caption"):
            text = item.get("text", "")
            md_lines.append(f"**{text}**")
            md_lines.append("")

        else:
            text = item.get("text", "")
            if text:
                md_lines.append(text)
                md_lines.append("")

    return "\n".join(md_lines)


# -----------------------
# 模型加载与推理
# -----------------------
def initialize_model(model_path: str, local_files_only: bool = True) -> None:
    """初始化模型"""
    global model, tokenizer, device

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


    tokenizer_kwargs = dict(trust_remote_code=True, local_files_only=local_files_only)
    model_kwargs = dict(trust_remote_code=True, local_files_only=local_files_only, use_safetensors=True)

    if _supports_bf16():
        model_kwargs["torch_dtype"] = torch.bfloat16
    elif torch.cuda.is_available():
        model_kwargs["torch_dtype"] = torch.float16
    else:
        model_kwargs["torch_dtype"] = torch.float32

    tokenizer = AutoTokenizer.from_pretrained(model_path, **tokenizer_kwargs)
    model = AutoModel.from_pretrained(model_path, **model_kwargs).eval().to(device)

    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        tokenizer.pad_token = tokenizer.eos_token



@torch.inference_mode()
def process_image_ocr(
    image: Image.Image,
    prompt: Optional[str] = None,
    base_size: int = 1024,
    image_size: int = 640,
    crop_mode: bool = True,
    verbose: bool = False,
) -> str:
    """使用 DeepSeek 的 model.infer 进行 OCR"""
    if model is None or tokenizer is None:
        raise RuntimeError("模型未初始化")
    if not hasattr(model, "infer"):
        raise RuntimeError("模型不含 infer 方法")

    if not prompt:
        prompt = "<image>\n<|grounding|>Convert the document to markdown."

    if image.mode != "RGB":
        image = image.convert("RGB")

    img_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img_tmp_path = img_tmp.name
    img_tmp.close()
    image.save(img_tmp_path, format="PNG")

    out_dir = tempfile.mkdtemp(prefix="dpsk_ocr_")
    try:
        out = model.infer(
            tokenizer=tokenizer,
            prompt=prompt,
            image_file=img_tmp_path,
            output_path=out_dir,
            base_size=base_size,
            image_size=image_size,
            crop_mode=crop_mode,
            save_results=False,
            test_compress=verbose,
            eval_mode=True,
        )
        result = out if isinstance(out, str) else str(out)
        if verbose:
            print("\n===== [DeepSeek-OCR raw output] =====", flush=True)
            print(result, flush=True)
            print("===== [End of raw output] =====\n", flush=True)
        return result
    finally:
        try: os.remove(img_tmp_path)
        except: pass
        try: shutil.rmtree(out_dir, ignore_errors=True)
        except: pass


# -----------------------
# FastAPI Routes
# -----------------------
@app.on_event("startup")
async def startup_event():
    print("=" * 60)
    print("DeepSeek OCR API 服务启动中...")
    print("=" * 60)


@app.get("/")
async def root():
    return {
        "service": "DeepSeek OCR API Service (MinerU-compatible)",
        "version": "1.0.0",
        "status": "running",
        "model_loaded": model is not None,
        "supported_formats": ["image", "pdf"],
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy" if model is not None else "initializing",
        "model_ready": model is not None,
        "device": str(device) if device else None,
    }


@app.post("/v1/ocr/image")
async def ocr_image(
    file: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    image_desc_prompt: Optional[str] = Form(None),
    enable_image_description: bool = Form(False),
    draw_bbox: bool = Form(False),  # ✅ 新增参数
    base_size: int = Form(1024),
    image_size: int = Form(640),
    crop_mode: bool = Form(True),
    verbose: bool = Form(False),
):
    """图片 OCR (返回 MinerU 兼容格式)"""
    if model is None:
        raise HTTPException(status_code=503, detail="模型未初始化")

    try:
        contents = await file.read()
        image = Image.open(BytesIO(contents)).convert("RGB")

        raw_text = process_image_ocr(
            image=image,
            prompt=prompt,
            base_size=base_size,
            image_size=image_size,
            crop_mode=crop_mode,
            verbose=verbose,
        )

        layout_dets_page, middle_json_page, content_list_items, images_base64, page_img_b64 = parse_deepseek_output_to_mineru(
            text=raw_text,
            page_idx=0,
            source_image=image,
            image_desc_prompt=image_desc_prompt,
            enable_image_description=enable_image_description,
            base_canvas_size=base_size,
            draw_bbox=draw_bbox,  # ✅ 传参
        )

        content_md = build_markdown_from_content_list(content_list_items, images_base64)

        result = {
            "backend": "deepseek-ocr",
            "version": "1.0.0",
            "results": {
                "test": {
                    "md_content": content_md,
                    "middle_json": {"pdf_info": [middle_json_page]},
                    "model_output": [layout_dets_page],
                    "content_list": content_list_items,
                    "images": images_base64,
                    "page_images": [page_img_b64] if page_img_b64 else []  # ✅ 新增字段
                }
            }
        }
        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {e}\n{tb}")


@app.post("/v1/ocr/pdf")
async def ocr_pdf(
    file: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    image_desc_prompt: Optional[str] = Form(None),
    enable_image_description: bool = Form(False),
    draw_bbox: bool = Form(False),  # ✅ 新增参数
    dpi: int = Form(144),
    base_size: int = Form(1024),
    image_size: int = Form(640),
    crop_mode: bool = Form(True),
    verbose: bool = Form(False),
):
    """PDF OCR (返回 MinerU 兼容格式)"""
    if model is None:
        raise HTTPException(status_code=503, detail="模型未初始化")

    try:
        contents = await file.read()
        images = pdf_to_images(contents, dpi=dpi)

        all_layout_dets = []
        all_middle_json_pages = []
        all_content_list = []
        all_images_base64 = {}
        all_md_parts = []
        all_page_images = []  # ✅ 新增

        for idx, image in enumerate(images):
            try:
                raw_text = process_image_ocr(
                    image=image,
                    prompt=prompt,
                    base_size=base_size,
                    image_size=image_size,
                    crop_mode=crop_mode,
                    verbose=verbose,
                )

                layout_dets_page, middle_json_page, content_list_items, images_base64, page_img_b64 = parse_deepseek_output_to_mineru(
                    text=raw_text,
                    page_idx=idx,
                    source_image=image,
                    image_desc_prompt=image_desc_prompt,
                    enable_image_description=enable_image_description,
                    base_canvas_size=base_size,
                    draw_bbox=draw_bbox,  # ✅ 传参
                )

                all_layout_dets.append(layout_dets_page)
                all_middle_json_pages.append(middle_json_page)
                all_content_list.extend(content_list_items)
                all_images_base64.update(images_base64)
                if page_img_b64:  # ✅ 收集带框页面图片
                    all_page_images.append(page_img_b64)

                page_md = build_markdown_from_content_list(content_list_items, images_base64)
                all_md_parts.append(page_md)

            except Exception as e:
                print(f"⚠️ 第 {idx+1} 页处理失败: {e}")
                import traceback; traceback.print_exc()
                all_layout_dets.append({
                    "layout_dets": [],
                    "page_info": {"page_no": idx, "width": image.width, "height": image.height}
                })
                all_middle_json_pages.append({
                    "page_idx": idx, "page_size": [image.width, image.height], "preproc_blocks": []
                })
                all_md_parts.append(f"[第 {idx + 1} 页处理失败: {str(e)}]")

        content_md = "\n\n".join(all_md_parts)

        result = {
            "backend": "deepseek-ocr",
            "version": "1.0.0",
            "results": {
                "test": {
                    "md_content": content_md,
                    "middle_json": {"pdf_info": all_middle_json_pages},
                    "model_output": all_layout_dets,
                    "content_list": all_content_list,
                    "images": all_images_base64,
                    "page_images": all_page_images  # ✅ 新增字段
                }
            }
        }
        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"PDF 处理失败: {e}\n{tb}")


@app.post("/v1/ocr/base64")
async def ocr_base64(
    image_base64: str,
    prompt: Optional[str] = None,
    image_desc_prompt: Optional[str] = None,
    enable_image_description: bool = False,
    draw_bbox: bool = Form(False),  # ✅ 新增参数
    base_size: int = 1024,
    image_size: int = 640,
    crop_mode: bool = True,
):
    """Base64 图片 OCR"""
    if model is None:
        raise HTTPException(status_code=503, detail="模型未初始化")

    try:
        image_data = base64.b64decode(image_base64)
        image = Image.open(BytesIO(image_data)).convert("RGB")

        raw_text = process_image_ocr(
            image=image,
            prompt=prompt,
            base_size=base_size,
            image_size=image_size,
            crop_mode=crop_mode,
        )

        layout_dets_page, middle_json_page, content_list_items, images_base64_dict, page_img_b64 = parse_deepseek_output_to_mineru(
            text=raw_text,
            page_idx=0,
            source_image=image,
            image_desc_prompt=image_desc_prompt,
            enable_image_description=enable_image_description,
            base_canvas_size=base_size,
            draw_bbox=draw_bbox,  # ✅ 传参
        )

        content_md = build_markdown_from_content_list(content_list_items, images_base64_dict)

        result = {
            "backend": "deepseek-ocr",
            "version": "1.0.0",
            "results": {
                "test": {
                    "md_content": content_md,
                    "middle_json": {"pdf_info": [middle_json_page]},
                    "model_output": [layout_dets_page],
                    "content_list": content_list_items,
                    "images": images_base64_dict,
                    "page_images": [page_img_b64] if page_img_b64 else []  # ✅ 新增字段
                }
            }
        }
        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


@app.post("/v1/ocr/batch")
async def ocr_batch(
    files: List[UploadFile] = File(...),
    prompt: Optional[str] = Form(None),
    image_desc_prompt: Optional[str] = Form(None),
    enable_image_description: bool = Form(False),
    draw_bbox: bool = Form(False),  # ✅ 新增参数
    base_size: int = Form(1024),
    image_size: int = Form(640),
    crop_mode: bool = Form(True),
    verbose: bool = Form(False),
):
    """批量 OCR"""
    if model is None:
        raise HTTPException(status_code=503, detail="模型未初始化")

    try:
        batch_result = {
            "backend": "deepseek-ocr",
            "version": "1.0.0",
            "results": {
                "test": {}
            }
        }

        for file in files:
            contents = await file.read()

            if file.filename.lower().endswith(".pdf"):
                images = pdf_to_images(contents, dpi=144)
                all_layout_dets = []
                all_middle_json_pages = []
                all_content_list = []
                images_merged = {}
                all_md_parts = []
                all_page_images = []  # ✅ 新增

                for idx, image in enumerate(images):
                    raw_text = process_image_ocr(
                        image=image,
                        prompt=prompt,
                        base_size=base_size,
                        image_size=image_size,
                        crop_mode=crop_mode,
                        verbose=verbose,
                    )

                    layout_dets_page, middle_json_page, content_list_items, images_base64, page_img_b64 = parse_deepseek_output_to_mineru(
                        text=raw_text,
                        page_idx=idx,
                        source_image=image,
                        image_desc_prompt=image_desc_prompt,
                        enable_image_description=enable_image_description,
                        base_canvas_size=base_size,
                        draw_bbox=draw_bbox,  # ✅ 传参
                    )

                    all_layout_dets.append(layout_dets_page)
                    all_middle_json_pages.append(middle_json_page)
                    all_content_list.extend(content_list_items)
                    images_merged.update(images_base64)
                    if page_img_b64:  # ✅ 收集
                        all_page_images.append(page_img_b64)

                    page_md = build_markdown_from_content_list(content_list_items, images_base64)
                    all_md_parts.append(page_md)

                content_md = "\n\n".join(all_md_parts)

                batch_result["results"]["test"] = {
                    "md_content": content_md,
                    "middle_json": {"pdf_info": all_middle_json_pages},
                    "model_output": all_layout_dets,
                    "content_list": all_content_list,
                    "images": images_merged,
                    "page_images": all_page_images  # ✅ 新增字段
                }

            else:
                image = Image.open(BytesIO(contents)).convert("RGB")

                raw_text = process_image_ocr(
                    image=image,
                    prompt=prompt,
                    base_size=base_size,
                    image_size=image_size,
                    crop_mode=crop_mode,
                    verbose=verbose,
                )

                layout_dets_page, middle_json_page, content_list_items, images_base64, page_img_b64 = parse_deepseek_output_to_mineru(
                    text=raw_text,
                    page_idx=0,
                    source_image=image,
                    image_desc_prompt=image_desc_prompt,
                    enable_image_description=enable_image_description,
                    base_canvas_size=base_size,
                    draw_bbox=draw_bbox,  # ✅ 传参
                )

                content_md = build_markdown_from_content_list(content_list_items, images_base64)

                batch_result["results"]["test"] = {
                    "md_content": content_md,
                    "middle_json": {"pdf_info": [middle_json_page]},
                    "model_output": [layout_dets_page],
                    "content_list": content_list_items,
                    "images": images_base64,
                    "page_images": [page_img_b64] if page_img_b64 else []  # ✅ 新增字段
                }

        return JSONResponse(content=batch_result)

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"批量处理失败: {e}\n{tb}")

# -----------------------
# Main Entry Point
# -----------------------
def main():
    parser = argparse.ArgumentParser(description="DeepSeek OCR API Server (MinerU-compatible)")
    parser.add_argument("--model-path", type=str, required=True, help="模型路径")
    parser.add_argument("--gpu-id", type=int, default=0, help="GPU ID (default: 0)")
    parser.add_argument("--port", type=int, default=8705, help="服务端口 (default: 8705)")
    parser.add_argument("--host", type=str, default=os.getenv("DEEPSEEK_OCR_HOST", "127.0.0.1"), help="服务地址 (default: 127.0.0.1)")
    parser.add_argument("--local-files-only", action="store_true", help="仅使用本地文件加载模型")
    parser.add_argument("--workers", type=int, default=1, help="Worker 数量 (default: 1)")
    
    args = parser.parse_args()
    
    # 设置 CUDA 设备
    if torch.cuda.is_available():
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_id)
        print(f"🎮 使用 GPU: {args.gpu_id}")
    else:
        print("⚠️ CUDA 不可用，将使用 CPU")
    
    # 初始化模型
    try:
        initialize_model(args.model_path, local_files_only=args.local_files_only)
    except Exception as e:
        print(f"❌ 模型加载失败: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # 启动服务
    print("\n" + "=" * 60)
    print(f"🚀 启动 DeepSeek OCR API 服务")
    print(f"📡 地址: http://{args.host}:{args.port}")
    print(f"📚 文档: http://{args.host}:{args.port}/docs")
    print("=" * 60 + "\n")
    
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
