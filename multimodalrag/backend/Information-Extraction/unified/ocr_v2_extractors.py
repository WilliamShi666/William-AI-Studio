"""
OCR 2.0 提取器
支持 MinerU, PaddleOCR, DeepSeek OCR 三种方法
"""

import os
import json
import requests
import tempfile
import base64
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, Optional, List
from abc import ABC, abstractmethod
from dotenv import load_dotenv

# PIL 图像处理
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("警告: PIL 未安装，可视化功能将不可用")

# pypdfium2 PDF渲染
try:
    import pypdfium2
    PYPDFIUM2_AVAILABLE = True
except ImportError:
    PYPDFIUM2_AVAILABLE = False
    print("警告: pypdfium2 未安装，PDF渲染功能将不可用")

# 加载环境变量（统一使用 backend/.env）
# 本文件位于 backend/Information-Extraction/unified/
env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)


def _get_timeout_from_env(env_var: str, default: Optional[str] = None) -> Optional[float]:
    """
    将环境变量解析为可选的超时时间；返回None表示不限制超时。
    接受空字符串、'0'、'none'、'null'、'off'来禁用超时。
    """
    raw_value = os.getenv(env_var, default)
    if raw_value is None:
        return None

    value = raw_value.strip().lower()
    if value in ("", "0", "none", "null", "off"):
        return None

    try:
        timeout = float(value)
        return timeout if timeout > 0 else None
    except ValueError:
        print(f"警告: 环境变量 {env_var} 的值无法解析为数字 ({raw_value})，将禁用超时")
        return None



class BaseOCRExtractor(ABC):
    """OCR提取器基类"""
    
    @abstractmethod
    async def extract(self, pdf_path: Path) -> Dict[str, Any]:
        """
        提取PDF内容
        
        Args:
            pdf_path: PDF文件路径
            
        Returns:
            提取结果字典，包含:
            - markdown: 提取的markdown文本
            - total_pages: 总页数
            - total_images: 图片数量
            - layout_info: 版面信息（可选）
        """
        pass


class MinerUExtractor(BaseOCRExtractor):
    """MinerU提取器 - 增强版（支持可视化）"""
    
    def __init__(self):
        self.api_url = os.getenv("MINERU_API_URL", "http://localhost:10010/file_parse")
        self.vllm_url = os.getenv("VLLM_SERVER_URL", "http://localhost:30000")
        self.backend = os.getenv("MINERU_BACKEND", "pipeline")
        self.timeout = _get_timeout_from_env("MINERU_TIMEOUT")
        
        # 可视化输出目录
        self.viz_base_dir = Path(os.getenv(
            "MINERU_VIZ_DIR",
            "./mineru_visualizations"
        ))
        self.viz_base_dir.mkdir(parents=True, exist_ok=True)
        
    async def extract(self, pdf_path: Path) -> Dict[str, Any]:
        """使用MinerU提取PDF（含可视化）"""
        try:
            # 1. 调用MinerU API
            with open(pdf_path, 'rb') as f:
                files = [('files', (pdf_path.name, f, 'application/pdf'))]
                data = {
                    'backend': self.backend,
                    'server_url': self.vllm_url,
                    'parse_method': 'auto',
                    'lang_list': 'ch',
                    'return_md': 'true',
                    'return_middle_json': 'true',
                    'return_model_output': 'true',
                    'return_content_list': 'true',
                    'start_page_id': '0',
                    'end_page_id': '99999',
                }
                
                response = requests.post(
                    self.api_url,
                    files=files,
                    data=data,
                    timeout=self.timeout
                )
                
            if response.status_code != 200:
                raise Exception(f"MinerU API返回错误: {response.status_code}")
            
            # 2. 解析返回结果
            if response.headers.get("content-type", "").startswith("application/json"):
                file_json = response.json()
            else:
                file_json = json.loads(response.text)
            
            # 提取顶层信息
            backend = file_json.get("backend", self.backend)
            version = file_json.get("version", "2.5.4")
            
            # 提取结果数据（文件名作为key，去掉.pdf后缀）
            results = file_json.get("results", {})
            if not results:
                raise Exception("MinerU返回results为空")
            
            # 获取第一个结果（通常只有一个PDF文件）
            file_key = list(results.keys())[0] if results else None
            if not file_key:
                raise Exception("MinerU返回结果为空")
            
            res = results[file_key]
            
            # 解析各个部分
            md_content = res.get("md_content", "")
            
            # 解析JSON字符串
            def safe_json_loads(text):
                if not isinstance(text, str):
                    return text
                try:
                    return json.loads(text.strip())
                except:
                    return None
            
            middle_json = safe_json_loads(res.get("middle_json"))
            model_output = safe_json_loads(res.get("model_output"))
            content_list = safe_json_loads(res.get("content_list"))
            
            # 统计信息
            total_pages = len(middle_json.get("pdf_info", [])) if middle_json else 0
            total_images = self._count_images(middle_json)
                        
            # 3. 生成可视化（异步执行，不阻塞主流程）
            viz_paths = None
            try:
                viz_paths = await self._create_visualizations(
                    pdf_path, 
                    middle_json, 
                    model_output, 
                    content_list
                )
            except Exception as viz_error:
                raise Exception(f"MinerU可视化失败: {viz_error}")
            # 4. 填充 images 和 page_images 字段（如果可视化成功）
            images_dict = {}
            page_images_list = []
            
            if viz_paths:
                try:
                    # 填充 page_images（标注后的完整页面图片）
                    page_images_list = self._encode_page_images(viz_paths)
                    
                    # 填充 images（裁切后的图片/表格区域）
                    images_dict = self._encode_cropped_images(
                        pdf_path, 
                        content_list, 
                        middle_json,
                        viz_paths
                    )
                    
                    print(f"[MinerU] 已编码 {len(page_images_list)} 张页面图片, {len(images_dict)} 张裁切图片")
                except Exception as img_error:
                    print(f"[MinerU] 图片编码失败（不影响主流程）: {img_error}")
            
            # 5. 返回结果
            return {
                'markdown': md_content,
                'total_pages': total_pages,
                'total_images': total_images,
                'layout_info': {
                    'middle_json': middle_json,
                    'model_output': model_output,
                    'content_list': content_list,
                },
                'visualization_paths': viz_paths,  # 可视化图片路径
                'raw_data': {
                    # 保留原始 API 返回的所有字段（md_content, middle_json, model_output, content_list）
                    'md_content': md_content,
                    'middle_json': middle_json,  # 已解析的对象（不是字符串）
                    'model_output': model_output,  # 已解析的对象
                    'content_list': content_list,  # 已解析的对象
                    # 添加顶层信息
                    'backend': backend,
                    'version': version,
                    # 添加图片数据
                    'images': images_dict,
                    'page_images': page_images_list,
                }
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception(f"MinerU提取失败: {str(e)}")
    
    def _count_images(self, middle_json: dict) -> int:
        """统计图片数量"""
        if not middle_json:
            return 0
        
        count = 0
        pdf_info = middle_json.get("pdf_info", [])
        for page in pdf_info:
            for block in page.get("preproc_blocks", []):
                if block.get("type") == "image":
                    count += 1
        return count
    
    async def _create_visualizations(
        self, 
        pdf_path: Path, 
        middle_json: dict, 
        model_output: Any,
        content_list: Any
    ) -> List[Dict[str, str]]:
        """生成可视化标注图片"""
        try:
            # 创建输出目录
            file_stem = pdf_path.stem
            out_dir = self.viz_base_dir / file_stem
            out_dir.mkdir(parents=True, exist_ok=True)
            
            # 渲染PDF页面
            doc = pypdfium2.PdfDocument(str(pdf_path))
            viz_paths = []
            
            for page_idx in range(min(len(doc), 10)):  # 最多处理前10页
                page = doc.get_page(page_idx)
                pil_img = page.render(scale=1.5).to_pil()
                
                # 保存原始页面
                raw_path = out_dir / f"page_{page_idx:03d}_raw.jpg"
                pil_img.save(raw_path, "JPEG", quality=92)
                
                # 创建标注版本
                annotated_path = out_dir / f"page_{page_idx:03d}_annotated.jpg"
                self._overlay_annotations(
                    pil_img, 
                    page_idx, 
                    model_output,
                    annotated_path
                )
                
                viz_paths.append({
                    'page': page_idx,
                    'raw': str(raw_path),
                    'annotated': str(annotated_path)
                })
            
            return viz_paths
            
        except Exception as e:
            print(f"可视化生成失败: {e}")
            return []
    
    def _overlay_annotations(
        self, 
        img: Image.Image, 
        page_idx: int,
        model_output: Any,
        out_path: Path
    ):
        """在图片上叠加标注框（参考 MinerU demo 实现）"""
        try:
            draw = ImageDraw.Draw(img)
            W_img, H_img = img.size
            
            # 获取页面数据
            if not model_output or not isinstance(model_output, list):
                img.save(out_path, "JPEG", quality=92)
                return
            
            page_item = None
            if page_idx < len(model_output):
                page_item = model_output[page_idx]
            
            if not isinstance(page_item, dict):
                img.save(out_path, "JPEG", quality=92)
                return
            
            # 获取源页面尺寸
            page_info = page_item.get("page_info", {})
            W_src = page_info.get("width", W_img)
            H_src = page_info.get("height", H_img)
            
            sx = W_img / W_src if W_src else 1.0
            sy = H_img / H_src if H_src else 1.0
            
            # 类别名称映射（MinerU 官方定义）
            category_names = {
                0: "title",
                1: "text",
                2: "abandon",
                3: "image",
                4: "image_caption",
                5: "table",
                6: "table_caption",
                7: "table_footnote",
                8: "isolate_formula",
                9: "formula_caption",
                13: "inline_formula",
                14: "isolate_formula",
                15: "ocr_text",
            }
            
            # 颜色映射
            def get_color(cat_id):
                if cat_id == 0:  # title
                    return (230, 57, 70)  # 红
                elif cat_id == 1:  # text
                    return (66, 135, 245)  # 蓝
                elif cat_id in (3, 4):  # image, image_caption
                    return (247, 159, 36)  # 橙
                elif cat_id in (5, 6, 7):  # table, table_caption, table_footnote
                    return (39, 174, 96)  # 绿
                elif cat_id in (8, 9, 13, 14):  # formulas
                    return (155, 89, 182)  # 紫
                elif cat_id in (2, 15):  # abandon, ocr_text
                    return (200, 200, 200)  # 灰
                else:
                    return (0, 0, 0)  # 黑
            
            # 获取字体
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
            except:
                font = ImageFont.load_default()
            
            # 绘制检测框（按score排序，低分先画）
            layout_dets = page_item.get("layout_dets", [])
            for det in sorted(layout_dets, key=lambda x: x.get("score", 0)):
                cat_id = det.get("category_id")
                poly = det.get("poly")
                bbox = det.get("bbox")
                score = det.get("score", 0)
                
                # 跳过不需要显示的类别
                # 2: abandon (页眉页脚)
                # 15: ocr_text (细粒度OCR文本，太密集)
                if cat_id in [2, 15]:
                    continue
                
                # 获取类型名称和颜色
                type_name = category_names.get(cat_id, f"unknown_{cat_id}")
                color = get_color(cat_id)
                label = f"{type_name} ({score:.2f})"
                
                # 优先使用 poly，然后才是 bbox
                if poly and len(poly) >= 6:
                    # 绘制多边形
                    scaled_poly = [(int(poly[i] * sx), int(poly[i+1] * sy)) 
                                   for i in range(0, len(poly), 2)]
                    draw.line(scaled_poly + [scaled_poly[0]], fill=color, width=4)
                    
                    # 在第一个点绘制标签
                    if scaled_poly:
                        self._draw_label(draw, scaled_poly[0], label, color, font)
                        
                elif bbox and len(bbox) >= 4:
                    # 绘制矩形
                    x1, y1, x2, y2 = bbox[:4]
                    scaled_bbox = [
                        int(x1 * sx), 
                        int(y1 * sy), 
                        int(x2 * sx), 
                        int(y2 * sy)
                    ]
                    draw.rectangle(scaled_bbox, outline=color, width=4)
                    
                    # 绘制标签
                    self._draw_label(draw, (scaled_bbox[0], scaled_bbox[1]), label, color, font)
            
            img.save(out_path, "JPEG", quality=92)
            
        except Exception as e:
            print(f"标注失败: {e}")
            import traceback
            traceback.print_exc()
            img.save(out_path, "JPEG", quality=92)
    
    def _draw_label(self, draw, pos, label, color, font):
        """绘制标签"""
        try:
            x, y = pos
            
            # 获取文字尺寸
            try:
                bbox = draw.textbbox((0, 0), label, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
            except AttributeError:
                text_width, text_height = draw.textsize(label, font=font)
            
            # 标签位置（框的上方）
            label_x = x
            label_y = y - text_height - 4
            
            # 如果超出上边界，放在框内部
            if label_y < 0:
                label_y = y + 2
            
            # 绘制标签背景
            padding = 2
            bg_bbox = [
                label_x - padding,
                label_y - padding,
                label_x + text_width + padding,
                label_y + text_height + padding
            ]
            draw.rectangle(bg_bbox, fill=color)
            
            # 绘制标签文字（白色）
            draw.text((label_x, label_y), label, fill=(255, 255, 255), font=font)
        except Exception as e:
            pass  # 标签绘制失败不影响主流程
    
    def _encode_page_images(self, viz_paths: List[Dict[str, Any]]) -> List[str]:
        """
        编码标注后的完整页面图片为 base64
        
        Args:
            viz_paths: 可视化图片路径列表
            
        Returns:
            base64 编码的图片列表
        """
        if not PIL_AVAILABLE:
            return []
        
        page_images = []
        
        for viz_item in viz_paths:
            annotated_path = viz_item.get('annotated')
            if not annotated_path or not Path(annotated_path).exists():
                continue
            
            try:
                with open(annotated_path, 'rb') as f:
                    img_base64 = base64.b64encode(f.read()).decode('utf-8')
                    page_images.append(img_base64)
            except Exception as e:
                print(f"编码页面图片失败 {annotated_path}: {e}")
        
        return page_images
    
    def _encode_cropped_images(
        self, 
        pdf_path: Path, 
        content_list: List[Dict[str, Any]],
        middle_json: Dict[str, Any],
        viz_paths: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """
        裁切并编码图片/表格区域为 base64
        
        Args:
            pdf_path: 原始 PDF 路径
            content_list: 内容列表
            middle_json: 中间 JSON 数据
            viz_paths: 可视化图片路径列表
            
        Returns:
            {img_path: base64_string} 字典
        """
        if not PIL_AVAILABLE or not content_list:
            return {}
        
        images_dict = {}
        
        # 创建页面索引到原始图片的映射
        page_to_raw_img = {}
        for viz_item in viz_paths:
            page_idx = viz_item.get('page')
            raw_path = viz_item.get('raw')
            if page_idx is not None and raw_path and Path(raw_path).exists():
                page_to_raw_img[page_idx] = raw_path
        
        # 遍历 content_list 中的图片和表格
        for item in content_list:
            if not isinstance(item, dict):
                continue
            
            item_type = item.get("type", "")
            if item_type not in ["image", "table"]:
                continue
            
            img_path_str = item.get("img_path", "")
            bbox = item.get("bbox", [])
            page_idx = item.get("page_idx", 0)
            
            if not img_path_str or len(bbox) < 4:
                continue
            
            # 获取对应页面的原始图片
            raw_img_path = page_to_raw_img.get(page_idx)
            if not raw_img_path:
                continue
            
            try:
                # 打开页面图片
                page_img = Image.open(raw_img_path)
                W_img, H_img = page_img.size
                
                # 获取 PDF 页面尺寸
                W_pdf, H_pdf = self._get_page_size(page_idx, middle_json)
                if not W_pdf or not H_pdf:
                    W_pdf, H_pdf = 595, 841
                
                # 计算坐标转换参数
                SCALE_X, SCALE_Y, OFFSET_X, OFFSET_Y = self._calculate_transform_params(
                    page_idx, middle_json, content_list
                )
                
                # 坐标转换
                final_bbox = self._transform_bbox(
                    bbox, SCALE_X, SCALE_Y, OFFSET_X, OFFSET_Y, W_img, H_img, W_pdf, H_pdf
                )
                
                # 转换为整数坐标并裁切
                x1 = max(0, int(round(final_bbox[0])))
                y1 = max(0, int(round(final_bbox[1])))
                x2 = min(W_img, int(round(final_bbox[2])))
                y2 = min(H_img, int(round(final_bbox[3])))
                
                if x2 <= x1 or y2 <= y1:
                    continue
                
                cropped = page_img.crop((x1, y1, x2, y2))
                
                # 转换为 base64
                buffered = BytesIO()
                cropped.save(buffered, format="PNG")
                img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                
                # 保存到字典
                images_dict[img_path_str] = img_base64
                
            except Exception as e:
                print(f"裁切图片失败 {img_path_str}: {e}")
        
        return images_dict
    
    def _get_page_size(self, page_idx: int, middle_json: Dict[str, Any]) -> tuple:
        """从 middle_json 获取页面尺寸"""
        if not middle_json:
            return (None, None)
        
        pdf_info = middle_json.get("pdf_info", [])
        if page_idx >= len(pdf_info):
            return (None, None)
        
        page_info = pdf_info[page_idx]
        page_size = page_info.get("page_size", [])
        
        if isinstance(page_size, list) and len(page_size) >= 2:
            return (float(page_size[0]), float(page_size[1]))
        
        return (None, None)
    
    def _calculate_transform_params(
        self, 
        page_idx: int, 
        middle_json: Dict[str, Any],
        content_list: List[Dict[str, Any]]
    ) -> tuple:
        """计算坐标转换参数"""
        SCALE_X = SCALE_Y = 1.0
        OFFSET_X = OFFSET_Y = 0.0
        
        if not middle_json:
            return (SCALE_X, SCALE_Y, OFFSET_X, OFFSET_Y)
        
        pdf_info = middle_json.get("pdf_info", [])
        if page_idx >= len(pdf_info):
            return (SCALE_X, SCALE_Y, OFFSET_X, OFFSET_Y)
        
        middle_blocks = pdf_info[page_idx].get("preproc_blocks", [])
        content_items = [
            item for item in content_list 
            if isinstance(item, dict) and item.get("page_idx") == page_idx
        ]
        
        if middle_blocks and content_items:
            m_bbox = middle_blocks[0].get("bbox", [])
            c_bbox = content_items[0].get("bbox", [])
            
            if len(m_bbox) >= 4 and len(c_bbox) >= 4:
                SCALE_X = (c_bbox[2] - c_bbox[0]) / (m_bbox[2] - m_bbox[0]) if (m_bbox[2] - m_bbox[0]) > 0 else 1.0
                SCALE_Y = (c_bbox[3] - c_bbox[1]) / (m_bbox[3] - m_bbox[1]) if (m_bbox[3] - m_bbox[1]) > 0 else 1.0
                OFFSET_X = c_bbox[0] - m_bbox[0] * SCALE_X
                OFFSET_Y = c_bbox[1] - m_bbox[1] * SCALE_Y
        
        return (SCALE_X, SCALE_Y, OFFSET_X, OFFSET_Y)
    
    def _transform_bbox(
        self, 
        bbox: List[float], 
        SCALE_X: float, 
        SCALE_Y: float,
        OFFSET_X: float, 
        OFFSET_Y: float,
        W_img: int, 
        H_img: int,
        W_pdf: float, 
        H_pdf: float
    ) -> List[float]:
        """转换 bbox 坐标"""
        # 1. 去除偏移
        bbox_no_offset = [
            bbox[0] - OFFSET_X,
            bbox[1] - OFFSET_Y,
            bbox[2] - OFFSET_X,
            bbox[3] - OFFSET_Y
        ]
        
        # 2. 反缩放到 PDF 坐标系
        bbox_in_pdf = [
            bbox_no_offset[0] / SCALE_X,
            bbox_no_offset[1] / SCALE_Y,
            bbox_no_offset[2] / SCALE_X,
            bbox_no_offset[3] / SCALE_Y
        ]
        
        # 3. 缩放到图片坐标系
        sx = W_img / W_pdf
        sy = H_img / H_pdf
        final_bbox = [
            bbox_in_pdf[0] * sx,
            bbox_in_pdf[1] * sy,
            bbox_in_pdf[2] * sx,
            bbox_in_pdf[3] * sy
        ]
        
        return final_bbox


class PaddleOCRExtractor(BaseOCRExtractor):
    """PaddleOCR-VL提取器 - 兼容MinerU格式"""
    
    def __init__(self):
        # PaddleOCR-VL API配置
        self.api_url = os.getenv("PADDLEOCR_VL_API_URL", "http://localhost:8802/parse")
        self.timeout = _get_timeout_from_env("PADDLEOCR_VL_TIMEOUT")
        
        # 可视化输出目录
        self.viz_base_dir = Path(os.getenv(
            "MINERU_VIZ_DIR",
            "/home/MuyuWorkSpace/01_TrafficProject/Multimodal_RAG_OCR/backend/mineru_visualizations"
        ))
        self.viz_base_dir.mkdir(parents=True, exist_ok=True)
        
    async def extract(self, pdf_path: Path) -> Dict[str, Any]:
        """使用PaddleOCR-VL提取PDF（MinerU格式）"""
        try:
            print(f"[PaddleOCR-VL] 开始处理: {pdf_path.name}")
            print(f"[PaddleOCR-VL] 调用API: {self.api_url}")
            
            # 1. 调用PaddleOCR-VL API
            with open(pdf_path, 'rb') as f:
                files = {'file': (pdf_path.name, f, 'application/pdf')}
                
                response = requests.post(
                    self.api_url,
                    files=files,
                    timeout=self.timeout
                )
                
            if response.status_code != 200:
                raise Exception(f"PaddleOCR-VL API返回错误: {response.status_code}, {response.text[:500]}")
                
            # 2. 解析返回结果（MinerU格式）
            file_json = response.json()
            
            # 验证返回格式
            if not file_json.get('results'):
                raise Exception("PaddleOCR-VL返回格式错误: 缺少 results 字段")
            
            # 提取顶层信息
            backend = file_json.get("backend", "paddleocr-vl")
            version = file_json.get("version", "0.9B")
            
            # 获取结果数据
            results = file_json.get("results", {})
            file_key = list(results.keys())[0] if results else None
            
            if not file_key:
                raise Exception("PaddleOCR-VL返回结果为空")
            
            res = results[file_key]
            
            # 提取各个部分
            md_content = res.get("md_content", "")
            middle_json = res.get("middle_json", {})
            model_output = res.get("model_output", [])
            content_list = res.get("content_list", [])
            images = res.get("images", {})
            page_images = res.get("page_images", [])
            
            # 统计信息
            total_pages = len(model_output) if model_output else 0
            total_images = self._count_images_from_content_list(content_list)
            
            print(f"[PaddleOCR-VL] 解析完成: {total_pages}页, {total_images}张图片")
            print(f"  - model_output 页数: {len(model_output)}")
            print(f"  - content_list 条目: {len(content_list)}")
            print(f"  - images 数量: {len(images)}")
            print(f"  - page_images 数量: {len(page_images)}")
            
            # 3. 保存可视化图片
            viz_paths = []
            if page_images:
                try:
                    viz_paths = self._save_page_images(pdf_path, page_images)
                    print(f"[PaddleOCR-VL] 已保存 {len(viz_paths)} 张可视化图片")
                except Exception as viz_error:
                    print(f"[PaddleOCR-VL] 保存可视化图片失败（不影响主流程）: {viz_error}")
            
            # 4. 返回结果（与MinerU保持一致的格式）
            return {
                'markdown': md_content,
                'total_pages': total_pages,
                'total_images': total_images,
                'layout_info': {
                    'middle_json': middle_json,
                    'model_output': model_output,
                    'content_list': content_list,
                },
                'visualization_paths': viz_paths,
                'raw_data': {
                    'md_content': md_content,
                    'middle_json': middle_json,
                    'model_output': model_output,
                    'content_list': content_list,
                    'backend': backend,
                    'version': version,
                    'images': images,  # Dict[str, str] - 裁切的图片/表格区域（base64）
                    'page_images': page_images,  # List[str] - 完整页面可视化（base64）
                }
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception(f"PaddleOCR-VL提取失败: {str(e)}")
    
    def _count_images_from_content_list(self, content_list: list) -> int:
        """从content_list统计图片数量"""
        count = 0
        for item in content_list:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if "image" in item_type.lower() or "figure" in item_type.lower():
                    count += 1
        return count
    
    def _save_page_images(self, pdf_path: Path, page_images: List[str]) -> List[Dict[str, str]]:
        """保存可视化页面图片"""
        if not PIL_AVAILABLE:
            return []
        
        try:
            # 创建输出目录
            file_stem = pdf_path.stem
            out_dir = self.viz_base_dir / f"{file_stem}_paddleocr"
            out_dir.mkdir(parents=True, exist_ok=True)
            
            viz_paths = []
            
            for idx, page_img_b64 in enumerate(page_images):
                if not page_img_b64:
                    continue
                
                try:
                    # 解码base64
                    img_data = base64.b64decode(page_img_b64)
                    
                    # 保存为图片
                    annotated_path = out_dir / f"page_{idx:03d}_annotated.png"
                    with open(annotated_path, 'wb') as f:
                        f.write(img_data)
                    
                    viz_paths.append({
                        'page': idx,
                        'annotated': str(annotated_path)
                    })
                    
                except Exception as e:
                    print(f"[PaddleOCR-VL] 保存页面图片 {idx} 失败: {e}")
            
            return viz_paths
            
        except Exception as e:
            print(f"[PaddleOCR-VL] 保存页面图片失败: {e}")
            return []


class DeepSeekOCRExtractor(BaseOCRExtractor):
    """DeepSeek OCR提取器 - 兼容MinerU格式"""
    
    def __init__(self):
        self.api_url = os.getenv("DEEPSEEK_OCR_API_URL", "http://localhost:8705/v1/ocr/pdf")
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.dpi = int(os.getenv("DEEPSEEK_OCR_DPI", "144"))
        self.base_size = int(os.getenv("DEEPSEEK_OCR_BASE_SIZE", "1024"))
        self.image_size = int(os.getenv("DEEPSEEK_OCR_IMAGE_SIZE", "640"))
        self.enable_image_description = os.getenv("DEEPSEEK_ENABLE_IMAGE_DESC", "true").lower() == "true"
        self.draw_bbox = os.getenv("DEEPSEEK_DRAW_BBOX", "true").lower() == "true"
        self.timeout = _get_timeout_from_env("DEEPSEEK_OCR_TIMEOUT")
        
    async def extract(self, pdf_path: Path) -> Dict[str, Any]:
        """使用DeepSeek OCR提取PDF（MinerU格式）"""
        try:
            # 1. 调用DeepSeek OCR API
            with open(pdf_path, 'rb') as f:
                files = {'file': (pdf_path.name, f, 'application/pdf')}
                
                data = {
                    'dpi': str(self.dpi),
                    'base_size': str(self.base_size),
                    'image_size': str(self.image_size),
                    'crop_mode': 'true',
                    'verbose': 'false',
                    'enable_image_description': 'true' if self.enable_image_description else 'false',
                    'draw_bbox': 'true' if self.draw_bbox else 'false'
                }
                
                headers = {}
                if self.api_key:
                    headers['Authorization'] = f'Bearer {self.api_key}'
                        
                response = requests.post(
                    self.api_url,
                    files=files,
                    data=data,
                    headers=headers,
                    timeout=self.timeout
                )
                
            if response.status_code != 200:
                raise Exception(f"DeepSeek OCR API返回错误: {response.status_code}, {response.text[:500]}")
                
            # 2. 解析返回结果（MinerU格式）
            file_json = response.json()
            
            # 验证返回格式
            if not file_json.get('results'):
                raise Exception("DeepSeek OCR返回格式错误: 缺少 results 字段")
            
            # 提取顶层信息
            backend = file_json.get("backend", "deepseek")
            version = file_json.get("version", "1.0.0")
            
            # 获取结果数据（文件名作为key，去掉.pdf后缀）
            results = file_json.get("results", {})
            file_key = list(results.keys())[0] if results else None
            
            if not file_key:
                raise Exception("DeepSeek OCR返回结果为空")
            
            res = results[file_key]
            
            # 提取各个部分（DeepSeek已经返回解析后的对象，不需要再json.loads）
            md_content = res.get("md_content", "")
            middle_json = res.get("middle_json", {})
            model_output = res.get("model_output", [])
            content_list = res.get("content_list", [])
            images = res.get("images", {})
            page_images = res.get("page_images", [])
            
            # 统计信息
            total_pages = len(middle_json.get("pdf_info", [])) if middle_json else 0
            total_images = self._count_images_from_content_list(content_list)
            
            print(f"[DeepSeek OCR] 解析完成: {total_pages}页, {total_images}张图片")
            print(f"  - model_output 页数: {len(model_output)}")
            print(f"  - content_list 条目: {len(content_list)}")
            print(f"  - images 数量: {len(images)}")
            print(f"  - page_images 数量: {len(page_images)}")
            
            # 3. 保存带框的页面图片（可选）
            viz_paths = []
            if page_images and self.draw_bbox:
                try:
                    viz_paths = self._save_page_images(pdf_path, page_images)
                    print(f"[DeepSeek OCR] 已保存 {len(viz_paths)} 张带框页面图片")
                except Exception as viz_error:
                    print(f"[DeepSeek OCR] 保存页面图片失败（不影响主流程）: {viz_error}")
            
            # 4. 返回结果（与MinerU保持一致的格式）
            return {
                'markdown': md_content,
                'total_pages': total_pages,
                'total_images': total_images,
                'layout_info': {
                    'middle_json': middle_json,
                    'model_output': model_output,
                    'content_list': content_list,
                },
                'visualization_paths': viz_paths,  # 带框页面图片路径
                'raw_data': {
                    # 保留完整的返回数据
                    'md_content': md_content,
                    'middle_json': middle_json,
                    'model_output': model_output,
                    'content_list': content_list,
                    # 添加顶层信息
                    'backend': backend,
                    'version': version,
                    # 图片数据（已经是base64）
                    'images': images,  # Dict[str, str] - 裁切的图片/表格区域
                    'page_images': page_images,  # List[str] - 完整页面（带框）
                }
            }
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception(f"DeepSeek OCR提取失败: {str(e)}")
    
    def _count_images_from_content_list(self, content_list: list) -> int:
        """从content_list统计图片数量"""
        count = 0
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "image":
                count += 1
        return count
    
    def _save_page_images(self, pdf_path: Path, page_images: List[str]) -> List[Dict[str, str]]:
        """保存带框的页面图片"""
        if not PIL_AVAILABLE:
            return []
        
        try:
            # 创建输出目录
            viz_base_dir = Path(os.getenv(
                "MINERU_VIZ_DIR",
                "/home/MuyuWorkSpace/01_TrafficProject/Multimodal_RAG_OCR/backend/mineru_visualizations"
            ))
            viz_base_dir.mkdir(parents=True, exist_ok=True)
            
            file_stem = pdf_path.stem
            out_dir = viz_base_dir / f"{file_stem}_deepseek"
            out_dir.mkdir(parents=True, exist_ok=True)
            
            viz_paths = []
            
            for idx, page_img_b64 in enumerate(page_images):
                if not page_img_b64:
                    continue
                
                try:
                    # 解码base64
                    img_data = base64.b64decode(page_img_b64)
                    
                    # 保存为图片
                    annotated_path = out_dir / f"page_{idx:03d}_annotated.png"
                    with open(annotated_path, 'wb') as f:
                        f.write(img_data)
                    
                    viz_paths.append({
                        'page': idx,
                        'annotated': str(annotated_path)
                    })
                    
                except Exception as e:
                    print(f"[DeepSeek OCR] 保存页面图片 {idx} 失败: {e}")
            
            return viz_paths
            
        except Exception as e:
            print(f"[DeepSeek OCR] 保存页面图片失败: {e}")
            return []


# OCR提取器工厂
def create_ocr_extractor(method: str) -> BaseOCRExtractor:
    """
    创建OCR提取器
    
    Args:
        method: OCR方法名称 ('mineru', 'paddleocr', 'paddleocr_vl', 'deepseek')
        
    Returns:
        对应的OCR提取器实例
    """
    extractors = {
        'mineru': MinerUExtractor,
        'paddleocr': PaddleOCRExtractor,
        'paddleocr_vl': PaddleOCRExtractor,  # PaddleOCR-VL 使用同一个提取器
        'deepseek': DeepSeekOCRExtractor,
    }
    
    extractor_class = extractors.get(method.lower())
    if not extractor_class:
        raise ValueError(f"不支持的OCR方法: {method}. 支持的方法: {list(extractors.keys())}")
    
    return extractor_class()


# 导出
__all__ = [
    'BaseOCRExtractor',
    'MinerUExtractor',
    'PaddleOCRExtractor',
    'DeepSeekOCRExtractor',
    'create_ocr_extractor'
]
