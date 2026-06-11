#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PaddleOCR-VL API 测试脚本
通过调用接口测试4个关键功能
"""

import requests
import json
import base64
import os
from pathlib import Path

# ============ 配置 ============
API_URL = os.getenv("PADDLEOCR_API_URL", "http://localhost:8802")
TEST_PDF = os.getenv("PADDLEOCR_TEST_PDF", "./input/test.pdf")
TEST_IMAGE = os.getenv("PADDLEOCR_TEST_IMAGE", "./input/sample.jpg")
OUTPUT_DIR = os.getenv("PADDLEOCR_TEST_OUTPUT_DIR", "./output")
# ===============================

def test_api():
    """测试API的4个关键功能"""
    
    output_dir = Path(OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print("PaddleOCR-VL API 测试")
    print("=" * 60)
    
    # 先检查健康状态
    print("\n检查服务状态...")
    try:
        health = requests.get(f"{API_URL}/health", timeout=5)
        print(f"✓ 服务正常: {health.json()}")
    except Exception as e:
        print(f"✗ 服务未启动: {e}")
        return
    
    # 测试 PDF
    print(f"\n处理PDF文件: {TEST_PDF}")
    if not Path(TEST_PDF).exists():
        print(f"✗ 文件不存在: {TEST_PDF}")
        return
    
    with open(TEST_PDF, "rb") as f:
        files = {"file": (Path(TEST_PDF).name, f, "application/pdf")}
        response = requests.post(f"{API_URL}/parse", files=files)
    
    if response.status_code != 200:
        print(f"✗ API调用失败: {response.text}")
        return
    
    result = response.json()
    
    # 1. 保存完整JSON
    json_file = output_dir / "api_result.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 1. 完整JSON已保存: {json_file}")
    
    # 获取results中的数据
    doc_name = list(result["results"].keys())[0]
    data = result["results"][doc_name]
    
    # 2. 保存md_content
    md_content = data["md_content"]
    md_file = output_dir / "md_content.md"
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✓ 2. Markdown已保存: {md_file}")
    print(f"   预览(前200字符): {md_content[:200]}...")
    
    # 3. 保存images中的base64图片
    images = data["images"]
    print(f"\n✓ 3. 保存images图片 (共{len(images)}张):")
    for idx, (img_name, img_base64) in enumerate(images.items(), 1):
        img_data = base64.b64decode(img_base64)
        img_file = output_dir / f"image_{idx}.png"
        with open(img_file, "wb") as f:
            f.write(img_data)
        print(f"   - {img_file.name} (来源: {img_name})")
    
    # 4. 保存page_images中的base64图片
    page_images = data["page_images"]
    print(f"\n✓ 4. 保存page_images图片 (共{len(page_images)}张):")
    for idx, page_img_base64 in enumerate(page_images, 1):
        img_data = base64.b64decode(page_img_base64)
        img_file = output_dir / f"page_{idx}.png"
        with open(img_file, "wb") as f:
            f.write(img_data)
        print(f"   - {img_file.name}")
    
    # 测试图片输入（如果有）
    if Path(TEST_IMAGE).exists():
        print(f"\n测试图片输入: {TEST_IMAGE}")
        with open(TEST_IMAGE, "rb") as f:
            files = {"file": (Path(TEST_IMAGE).name, f, "image/jpeg")}
            response = requests.post(f"{API_URL}/parse", files=files)
        
        if response.status_code == 200:
            print("✓ 支持图片输入")
            # 保存图片测试结果
            img_result = response.json()
            img_json_file = output_dir / "image_result.json"
            with open(img_json_file, "w", encoding="utf-8") as f:
                json.dump(img_result, f, ensure_ascii=False, indent=2)
            print(f"  结果已保存: {img_json_file}")
        else:
            print(f"✗ 图片处理失败: {response.text}")
    else:
        print(f"\n(跳过图片测试，未找到测试图片: {TEST_IMAGE})")
    
    # 总结
    print("\n" + "=" * 60)
    print("测试完成！输出文件:")
    print(f"  目录: {output_dir.absolute()}")
    print(f"  - api_result.json (完整JSON结果)")
    print(f"  - md_content.md (Markdown内容)")
    print(f"  - image_*.png ({len(images)}张内容图片)")
    print(f"  - page_*.png ({len(page_images)}张页面可视化)")
    print("=" * 60)

if __name__ == "__main__":
    test_api()
