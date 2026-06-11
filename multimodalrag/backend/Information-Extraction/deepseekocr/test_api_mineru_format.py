"""
DeepSeek OCR API 客户端测试 (MinerU 格式)
测试完整的三种输出格式: model_output / middle_json / content_list
"""
import requests
import json
import base64
from pathlib import Path
from typing import List, Optional, Dict, Any


def _safe_parse_response(response: requests.Response) -> dict:
    """安全解析响应"""
    try:
        return response.json()
    except Exception as e:
        return {
            "success": False, 
            "status_code": response.status_code, 
            "error": str(e),
            "text": response.text[:2000]
        }


class DeepSeekOCRClient:
    """DeepSeek OCR API 客户端"""
    
    def __init__(self, base_url: str = "http://localhost:8705"):
        self.base_url = base_url
    
    def health_check(self) -> dict:
        """健康检查"""
        r = requests.get(f"{self.base_url}/health")
        return _safe_parse_response(r)
    
    def ocr_image(
        self, 
        image_path: str, 
        prompt: Optional[str] = None,
        image_desc_prompt: Optional[str] = None,
        enable_image_description: bool = False,
        base_size: int = 1024,
        image_size: int = 640,
        crop_mode: bool = True,
        verbose: bool = True
    ) -> dict:
        """图片 OCR"""
        url = f"{self.base_url}/v1/ocr/image"
        
        with open(image_path, "rb") as f:
            mime = "image/png" if Path(image_path).suffix.lower() == ".png" else "image/jpeg"
            files = {"file": (Path(image_path).name, f, mime)}
            
            data = {
                "base_size": base_size,
                "image_size": image_size,
                "crop_mode": "true" if crop_mode else "false",
                "verbose": "true" if verbose else "false",
                "enable_image_description": "true" if enable_image_description else "false"
            }
            
            if prompt:
                data["prompt"] = prompt
            if image_desc_prompt:
                data["image_desc_prompt"] = image_desc_prompt
            
            r = requests.post(url, files=files, data=data, timeout=300)
        
        return _safe_parse_response(r)
    
    def ocr_pdf(
        self, 
        pdf_path: str, 
        prompt: Optional[str] = None,
        image_desc_prompt: Optional[str] = None,
        enable_image_description: bool = False,
        dpi: int = 144,
        base_size: int = 1024,
        image_size: int = 640,
        crop_mode: bool = True,
        verbose: bool = True,
        draw_bbox: bool = False  # 新增: 是否在图片上绘制框
    ) -> dict:
        """PDF OCR"""
        url = f"{self.base_url}/v1/ocr/pdf"
        
        with open(pdf_path, "rb") as f:
            files = {"file": (Path(pdf_path).name, f, "application/pdf")}
            
            data = {
                "dpi": dpi,
                "base_size": base_size,
                "image_size": image_size,
                "crop_mode": "true" if crop_mode else "false",
                "verbose": "true" if verbose else "false",
                "enable_image_description": "true" if enable_image_description else "false",
                "draw_bbox": "true" if draw_bbox else "false"  # 控制是否绘制框
            }
            
            if prompt:
                data["prompt"] = prompt
            if image_desc_prompt:
                data["image_desc_prompt"] = image_desc_prompt
            
            r = requests.post(url, files=files, data=data, timeout=600)
        
        return _safe_parse_response(r)
    
    def ocr_base64(
        self,
        image_base64: str,
        prompt: Optional[str] = None,
        image_desc_prompt: Optional[str] = None,
        enable_image_description: bool = False,
        base_size: int = 1024,
        image_size: int = 640,
        crop_mode: bool = True
    ) -> dict:
        """Base64 图片 OCR"""
        url = f"{self.base_url}/v1/ocr/base64"
        
        data = {
            "image_base64": image_base64,
            "base_size": base_size,
            "image_size": image_size,
            "crop_mode": crop_mode,
            "enable_image_description": enable_image_description
        }
        
        if prompt:
            data["prompt"] = prompt
        if image_desc_prompt:
            data["image_desc_prompt"] = image_desc_prompt
        
        r = requests.post(url, json=data, timeout=300)
        return _safe_parse_response(r)


def validate_mineru_format(result: dict) -> bool:
    """验证 MinerU 格式完整性"""
    print("\n" + "=" * 60)
    print("📋 验证 MinerU 格式")
    print("=" * 60)
    
    # 🔧 修正: 从 results.test 路径提取数据
    if "results" in result and "test" in result["results"]:
        test_data = result["results"]["test"]
    else:
        print("❌ 缺少 results.test 结构")
        return False
    
    checks = {
        "backend": "backend" in result,
        "version": "version" in result,
        "results": "results" in result and "test" in result["results"],
        "md_content": "md_content" in test_data and isinstance(test_data["md_content"], str),
        "model_output": "model_output" in test_data and isinstance(test_data["model_output"], list),
        "middle_json": "middle_json" in test_data and isinstance(test_data["middle_json"], dict),
        "content_list": "content_list" in test_data and isinstance(test_data["content_list"], list),
        "images": "images" in test_data and isinstance(test_data["images"], dict),
    }
    
    for key, passed in checks.items():
        status = "✅" if passed else "❌"
        print(f"{status} {key}: {passed}")
    
    # 详细验证
    if checks["model_output"]:
        print(f"\n📊 model_output 详情:")
        for idx, page in enumerate(test_data["model_output"]):
            layout_count = len(page.get("layout_dets", []))
            page_info = page.get("page_info", {})
            print(f"   页面 {idx}: {layout_count} 个 layout_dets, 尺寸 {page_info.get('width')}x{page_info.get('height')}")
    
    if checks["middle_json"]:
        print(f"\n📊 middle_json 详情:")
        pdf_info = test_data["middle_json"].get("pdf_info", [])
        for idx, page in enumerate(pdf_info):
            block_count = len(page.get("preproc_blocks", []))
            print(f"   页面 {idx}: {block_count} 个 preproc_blocks")
    
    if checks["content_list"]:
        print(f"\n📊 content_list 详情:")
        content_list = test_data["content_list"]
        print(f"   总条目数: {len(content_list)}")
        
        # 统计类型分布
        type_counts = {}
        for item in content_list:
            item_type = item.get("type", "unknown")
            type_counts[item_type] = type_counts.get(item_type, 0) + 1
        
        print(f"   类型分布:")
        for item_type, count in type_counts.items():
            print(f"      {item_type}: {count}")
    
    if checks["images"]:
        print(f"\n🖼️  图片详情:")
        images = test_data["images"]
        print(f"   图片数量: {len(images)}")
        for img_name, img_b64 in list(images.items())[:3]:  # 只显示前3个
            print(f"      {img_name}: {len(img_b64)} bytes (base64)")
    
    all_passed = all(checks.values())
    print(f"\n{'✅ 格式验证通过' if all_passed else '❌ 格式验证失败'}")
    print("=" * 60)
    
    return all_passed


def save_result(result: dict, output_dir: Path, prefix: str):
    """保存结果到文件"""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 🔧 修正: 从 results.test 提取数据
    if "results" in result and "test" in result["results"]:
        test_data = result["results"]["test"]
    else:
        print("⚠️  results.test 结构缺失,保存原始数据")
        test_data = result
    
    # 1. 保存完整 JSON
    json_path = output_dir / f"{prefix}_full.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"💾 完整 JSON: {json_path}")
    
    # 2. 保存 model_output
    if "model_output" in test_data:
        model_output_path = output_dir / f"{prefix}_model_output.json"
        with open(model_output_path, "w", encoding="utf-8") as f:
            json.dump(test_data["model_output"], f, ensure_ascii=False, indent=2)
        print(f"💾 model_output: {model_output_path}")
    
    # 3. 保存 middle_json
    if "middle_json" in test_data:
        middle_json_path = output_dir / f"{prefix}_middle_json.json"
        with open(middle_json_path, "w", encoding="utf-8") as f:
            json.dump(test_data["middle_json"], f, ensure_ascii=False, indent=2)
        print(f"💾 middle_json: {middle_json_path}")
    
    # 4. 保存 content_list
    if "content_list" in test_data:
        content_list_path = output_dir / f"{prefix}_content_list.json"
        with open(content_list_path, "w", encoding="utf-8") as f:
            json.dump(test_data["content_list"], f, ensure_ascii=False, indent=2)
        print(f"💾 content_list: {content_list_path}")
    
    # 5. 保存 Markdown
    if "md_content" in test_data:
        md_path = output_dir / f"{prefix}_content.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(test_data["md_content"])
        print(f"💾 Markdown: {md_path}")
    
    # 6. 保存图片 (base64 解码)
    if "images" in test_data:
        images_dir = output_dir / "images"
        images_dir.mkdir(exist_ok=True)
        
        for img_name, img_b64 in test_data["images"].items():
            try:
                img_data = base64.b64decode(img_b64)
                img_path = output_dir / img_name
                img_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(img_path, "wb") as f:
                    f.write(img_data)
                print(f"💾 图片: {img_path}")
            except Exception as e:
                print(f"⚠️  图片保存失败 {img_name}: {e}")
    
    # ✅ 7. 保存带框的页面图片
    if "page_images" in test_data and test_data["page_images"]:
        page_images_dir = output_dir / "page_images"
        page_images_dir.mkdir(exist_ok=True)
        
        for idx, page_img_b64 in enumerate(test_data["page_images"]):
            if page_img_b64:  # 确保不是空字符串
                try:
                    img_data = base64.b64decode(page_img_b64)
                    img_path = page_images_dir / f"page_{idx}_with_bbox.png"
                    
                    with open(img_path, "wb") as f:
                        f.write(img_data)
                    print(f"🖼️  带框页面图片: {img_path}")
                except Exception as e:
                    print(f"⚠️  页面图片保存失败 (页 {idx}): {e}")


def test_health():
    """测试 1: 健康检查"""
    print("\n" + "=" * 60)
    print("测试 1: 健康检查")
    print("=" * 60)
    
    client = DeepSeekOCRClient()
    
    try:
        health = client.health_check()
        print(f"✅ 健康检查成功")
        print(f"   状态: {health.get('status')}")
        print(f"   模型就绪: {health.get('model_ready')}")
        print(f"   设备: {health.get('device')}")
        return True
    except Exception as e:
        print(f"❌ 健康检查失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_image_ocr():
    """测试 2: 图片 OCR (不带图片描述)"""
    print("\n" + "=" * 60)
    print("测试 2: 图片 OCR (不带图片描述)")
    print("=" * 60)
    
    test_image = "/home/data/nongwa/workspace/data/图片3.jpg"
    
    if not Path(test_image).exists():
        print(f"⚠️  测试图片不存在: {test_image}")
        return
    
    client = DeepSeekOCRClient()
    
    try:
        print(f"📄 识别图片: {test_image}")
        print("⏳ 处理中...")
        
        result = client.ocr_image(
            test_image, 
            verbose=False,
            enable_image_description=False  # 不生成图片描述
        )
        
        if "error" in result:
            print(f"❌ 识别失败: {result['error']}")
            return
        
        print(f"✅ 识别成功")
        
        # 验证格式
        validate_mineru_format(result)
        
        # 保存结果
        output_dir = Path("output/test_image")
        save_result(result, output_dir, "image_ocr")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_image_ocr_with_description():
    """测试 3: 图片 OCR (带图片描述)"""
    print("\n" + "=" * 60)
    print("测试 3: 图片 OCR (带图片描述)")
    print("=" * 60)
    
    test_image = "/home/data/nongwa/workspace/data/图片3.jpg"
    
    if not Path(test_image).exists():
        print(f"⚠️  测试图片不存在: {test_image}")
        return
    
    client = DeepSeekOCRClient()
    
    try:
        print(f"📄 识别图片: {test_image}")
        print("⏳ 处理中 (启用图片描述生成)...")
        
        result = client.ocr_image(
            test_image, 
            verbose=False,
            enable_image_description=True,  # ✅ 启用图片描述
            image_desc_prompt="详细描述这张图片,包括主要内容、颜色、布局等特征。"
        )
        
        if "error" in result:
            print(f"❌ 识别失败: {result['error']}")
            return
        
        print(f"✅ 识别成功")
        
        # 验证格式
        validate_mineru_format(result)
        
        # 显示图片描述
        print("\n" + "=" * 60)
        print("🖼️  图片描述预览")
        print("=" * 60)
        for item in result.get("content_list", []):
            if item.get("type") == "image":
                captions = item.get("image_caption", [])
                if captions:
                    for caption in captions:
                        print(f"   {caption}")
        
        # 保存结果
        output_dir = Path("output/test_image_with_desc")
        save_result(result, output_dir, "image_ocr_with_desc")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_pdf_ocr():
    """测试 4: PDF OCR"""
    print("\n" + "=" * 60)
    print("测试 4: PDF OCR")
    print("=" * 60)
    
    test_pdf = "/home/data/nongwa/workspace/data/test.pdf"
    
    if not Path(test_pdf).exists():
        print(f"⚠️  测试 PDF 不存在: {test_pdf}")
        return
    
    client = DeepSeekOCRClient()
    
    try:
        print(f"📄 识别 PDF: {test_pdf}")
        print("⏳ 处理中...")
        
        result = client.ocr_pdf(
            test_pdf,
            dpi=144,
            verbose=False,
            enable_image_description=True,  # 生成图片描述
            draw_bbox=True  # ✅ 启用画框
        )
        
        if "error" in result:
            print(f"❌ 识别失败: {result['error']}")
            return
        
        print(f"✅ 识别成功")
        
        # 验证格式
        validate_mineru_format(result)
        
        # 保存结果
        output_dir = Path("output/test_pdf")
        save_result(result, output_dir, "pdf_ocr")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_base64_ocr():
    """测试 5: Base64 图片 OCR"""
    print("\n" + "=" * 60)
    print("测试 5: Base64 图片 OCR")
    print("=" * 60)
    
    test_image = "/home/data/nongwa/workspace/data/图片3.jpg"
    
    if not Path(test_image).exists():
        print(f"⚠️  测试图片不存在: {test_image}")
        return
    
    client = DeepSeekOCRClient()
    
    try:
        # 读取图片并转 base64
        with open(test_image, "rb") as f:
            image_data = f.read()
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        print(f"📄 识别 Base64 图片")
        print(f"   原始文件: {test_image}")
        print(f"   Base64 长度: {len(image_base64)}")
        print("⏳ 处理中...")
        
        result = client.ocr_base64(
            image_base64,
            enable_image_description=False
        )
        
        if "error" in result:
            print(f"❌ 识别失败: {result['error']}")
            return
        
        print(f"✅ 识别成功")
        
        # 验证格式
        validate_mineru_format(result)
        
        # 保存结果
        output_dir = Path("output/test_base64")
        save_result(result, output_dir, "base64_ocr")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def compare_with_mineru():
    """测试 6: 与 MinerU 输出格式对比"""
    print("\n" + "=" * 60)
    print("测试 6: 与 MinerU 格式对比")
    print("=" * 60)
    
    # 假设你有 MinerU 的输出文件
    mineru_content_list = "/home/data/nongwa/workspace/minerU/MinerU_2_5_4/demo/output/test/pipeline/content_list.json"
    mineru_model_output = "/home/data/nongwa/workspace/minerU/MinerU_2_5_4/demo/output/test/pipeline/model_output.json"
    
    test_pdf = "/home/data/nongwa/workspace/data/test.pdf"
    
    if not Path(test_pdf).exists():
        print(f"⚠️  测试 PDF 不存在: {test_pdf}")
        return
    
    client = DeepSeekOCRClient()
    
    try:
        # DeepSeek OCR
        print("🔄 执行 DeepSeek OCR...")
        deepseek_result = client.ocr_pdf(test_pdf, verbose=False, enable_image_description=False)
        
        if "error" in deepseek_result:
            print(f"❌ DeepSeek OCR 失败: {deepseek_result['error']}")
            return
        
        # 🔧 修正: 从 results.test 提取数据
        if "results" in deepseek_result and "test" in deepseek_result["results"]:
            test_data = deepseek_result["results"]["test"]
        else:
            print("❌ 缺少 results.test 结构")
            return
        
        # 加载 MinerU 结果
        mineru_cl = None
        mineru_mo = None
        
        if Path(mineru_content_list).exists():
            with open(mineru_content_list, "r", encoding="utf-8") as f:
                mineru_cl = json.load(f)
            print(f"✅ 加载 MinerU content_list: {len(mineru_cl)} 条目")
        
        if Path(mineru_model_output).exists():
            with open(mineru_model_output, "r", encoding="utf-8") as f:
                mineru_mo = json.load(f)
            print(f"✅ 加载 MinerU model_output: {len(mineru_mo)} 页")
        
        # 结构对比
        print("\n" + "=" * 60)
        print("📊 结构对比")
        print("=" * 60)
        
        print(f"\n1️⃣  content_list 对比:")
        if mineru_cl:
            print(f"   MinerU:    {len(mineru_cl)} 条目")
        print(f"   DeepSeek:  {len(test_data.get('content_list', []))} 条目")
        
        print(f"\n2️⃣  model_output 对比:")
        if mineru_mo:
            print(f"   MinerU:    {len(mineru_mo)} 页")
        print(f"   DeepSeek:  {len(test_data.get('model_output', []))} 页")
        
        # 字段对比 (取第一个条目)
        if mineru_cl and test_data.get('content_list'):
            print(f"\n3️⃣  content_list[0] 字段对比:")
            mineru_keys = set(mineru_cl[0].keys()) if mineru_cl else set()
            deepseek_keys = set(test_data['content_list'][0].keys())
            
            print(f"   MinerU 字段:   {sorted(mineru_keys)}")
            print(f"   DeepSeek 字段: {sorted(deepseek_keys)}")
            print(f"   共有字段:      {sorted(mineru_keys & deepseek_keys)}")
            print(f"   MinerU 独有:   {sorted(mineru_keys - deepseek_keys)}")
            print(f"   DeepSeek 独有: {sorted(deepseek_keys - mineru_keys)}")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def test_image_ocr_with_bbox():
    """测试 7: 图片 OCR (带框)"""
    print("\n" + "=" * 60)
    print("测试 7: 图片 OCR (带框)")
    print("=" * 60)
    
    test_image = "/home/data/nongwa/workspace/data/图片3.jpg"
    
    if not Path(test_image).exists():
        print(f"⚠️  测试图片不存在: {test_image}")
        return
    
    client = DeepSeekOCRClient()
    
    try:
        print(f"📄 识别图片: {test_image}")
        print("⏳ 处理中 (启用画框)...")
        
        result = client.ocr_image(
            test_image, 
            verbose=False,
            enable_image_description=False,
            draw_bbox=True  # ✅ 启用画框
        )
        
        if "error" in result:
            print(f"❌ 识别失败: {result['error']}")
            return
        
        print(f"✅ 识别成功")
        
        # 验证格式
        validate_mineru_format(result)
        
        # 保存结果
        output_dir = Path("output/test_image_with_bbox")
        save_result(result, output_dir, "image_ocr_with_bbox")
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


def main():
    """运行所有测试"""
    print("\n" + "🚀" * 30)
    print("DeepSeek OCR API 测试 (MinerU 格式)")
    print("🚀" * 30)
    
    # 1. 健康检查
    if not test_health():
        print("\n❌ 服务未就绪,停止测试")
        return
    
    # 2. 图片 OCR (不带描述)
    # test_image_ocr()
    
    # 3. 图片 OCR (带描述)
    # test_image_ocr_with_description()
    
    # 4. PDF OCR (带框)
    test_pdf_ocr()
    
    # 5. Base64 OCR
    # test_base64_ocr()
    
    # 6. 与 MinerU 格式对比
    # compare_with_mineru()
    
    # ✅ 7. 图片 OCR (带框)
    # test_image_ocr_with_bbox()
    
    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
