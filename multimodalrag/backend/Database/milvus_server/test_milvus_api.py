import requests
import json
import os
import time
from typing import Dict, Any

# 测试配置
BASE_URL = os.getenv("MILVUS_API_URL", "http://localhost:8000")
TEST_KB = "test_knowledge_base"

# 示例JSON数据（基于提供的样例）
test_json_path = "/home/data/nongwa/workspace/Text_segmentation/output/test/accurate_result_chunk.json"
with open(test_json_path, 'r', encoding='utf-8') as f:
    SAMPLE_JSON_DATA = json.load(f)

class KnowledgeBaseTestClient:
    """知识库测试客户端"""
    
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url
        
    def health_check(self) -> bool:
        """健康检查"""
        try:
            response = requests.get(f"{self.base_url}/health")
            return response.status_code == 200
        except:
            return False
    
    def create_knowledge_base(self, kb_name: str) -> Dict:
        """创建知识库"""
        response = requests.post(f"{self.base_url}/knowledge_base/create?collection_name={kb_name}")
        return response.json() if response.status_code == 200 else {"error": response.text}
    
    def delete_knowledge_base(self, kb_name: str) -> Dict:
        """删除知识库"""
        payload = {"collection_name": kb_name}
        response = requests.delete(f"{self.base_url}/knowledge_base/delete", json=payload)
        return response.json() if response.status_code == 200 else {"error": response.text}
    
    def list_knowledge_bases(self) -> Dict:
        """列出知识库"""
        response = requests.get(f"{self.base_url}/knowledge_base/list")
        return response.json() if response.status_code == 200 else {"error": response.text}
    
    def upload_json_file(self, kb_name: str, json_data: Dict) -> Dict:
        """上传JSON文件"""
        payload = {
            "collection_name": kb_name,
            "file_data": json_data
        }
        response = requests.post(f"{self.base_url}/upload_json", json=payload)
        return response.json() if response.status_code == 200 else {"error": response.text}
    
    def search_by_text(self, kb_name: str, query_text: str, top_k: int = 5) -> Dict:
        """根据问题搜索"""
        payload = {
            "collection_name": kb_name,
            "query_text": query_text,
            "top_k": top_k
        }
        response = requests.post(f"{self.base_url}/search", json=payload)
        return response.json() if response.status_code == 200 else {"error": response.text}
    
    def search_by_filename(self, kb_name: str, filename: str, top_k: int = 10) -> Dict:
        """根据文件名检索"""
        payload = {
            "collection_name": kb_name,
            "filename": filename,
            "top_k": top_k
        }
        response = requests.post(f"{self.base_url}/search_by_filename", json=payload)
        return response.json() if response.status_code == 200 else {"error": response.text}
    
    def delete_file(self, kb_name: str, filename: str) -> Dict:
        """删除文件"""
        payload = {
            "collection_name": kb_name,
            "filename": filename
        }
        response = requests.delete(f"{self.base_url}/delete", json=payload)
        return response.json() if response.status_code == 200 else {"error": response.text}

def run_comprehensive_test():
    """运行综合测试"""
    print("🚀 开始知识库综合测试")
    print("=" * 60)
    
    client = KnowledgeBaseTestClient()
    
    # 1. 健康检查
    print("1. 🔍 健康检查...")
    if not client.health_check():
        print("❌ 服务不可用，请确保服务已启动")
        return
    print("✅ 服务正常")
    
    # 2. 清理测试环境（删除可能存在的测试知识库）
    print("\n2. 🧹 清理测试环境...")
    client.delete_knowledge_base(TEST_KB)
    
    # 3. 创建知识库
    print("\n3. 📚 创建知识库...")
    result = client.create_knowledge_base(TEST_KB)
    print(f"   结果: {result}")
    
    # 4. 列出知识库
    print("\n4. 📋 列出知识库...")
    result = client.list_knowledge_bases()
    print(f"   知识库列表: {result.get('knowledge_bases', [])}")
    
    # 5. 上传JSON文件
    print("\n5. 📤 上传JSON文件...")
    result = client.upload_json_file(TEST_KB, SAMPLE_JSON_DATA)
    print(f"   上传结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
    
    if result.get("status") != "success":
        print("❌ 文件上传失败，终止测试")
        return
    
    # 等待数据索引
    print("\n   ⏳ 等待数据索引...")
    time.sleep(3)
    
    # 6. 根据问题搜索文本
    print("\n6. 🔍 根据问题搜索文本...")
    test_queries = [
        "什么动物",
        "基金经理",
        "华夏基金",
        "可爱的猫"
    ]
    
    for query in test_queries:
        print(f"\n   查询: '{query}'")
        result = client.search_by_text(TEST_KB, query, top_k=3)
        if result.get("status") == "success":
            print(f"   找到 {result.get('total', 0)} 个结果")
            for i, hit in enumerate(result.get("results", [])[:2], 1):
                print(f"     {i}. 相似度: {hit['score']:.3f}")
                print(f"        文本: {hit['chunk_text'][:100]}...")
        else:
            print(f"   ❌ 搜索失败: {result}")
    
    # 7. 根据文件名检索
    print("\n7. 📄 根据文件名检索...")
    filename = "test.pdf"  # 从JSON数据中提取的文件名
    result = client.search_by_filename(TEST_KB, filename)
    if result.get("status") == "success":
        print(f"   找到 {result.get('total', 0)} 个chunks")
        for i, chunk in enumerate(result.get("results", [])[:3], 1):
            print(f"     {i}. 页面: {chunk['metadata'].get('page_start', 'N/A')}")
            print(f"        文本: {chunk['chunk_text'][:80]}...")
    else:
        print(f"   ❌ 检索失败: {result}")
    
    # 8. 删除文件
    print("\n8. 🗑️  删除文件...")
    result = client.delete_file(TEST_KB, filename)
    print(f"   删除结果: {result}")
    
    # 9. 验证删除结果
    print("\n9. ✅ 验证删除结果...")
    time.sleep(1)
    result = client.search_by_filename(TEST_KB, filename)
    if result.get("total", 0) == 0:
        print("   ✅ 文件删除成功")
    else:
        print(f"   ⚠️  文件可能未完全删除，仍有 {result.get('total', 0)} 个chunks")
    
    # 10. 删除知识库
    print("\n10. 🗑️ 删除知识库...")
    result = client.delete_knowledge_base(TEST_KB)
    print(f"    删除结果: {result}")
    
    print("\n" + "=" * 60)
    print("🎉 测试完成!")

def test_error_cases():
    """测试错误情况"""
    print("\n🧪 测试错误情况")
    print("=" * 40)
    
    client = KnowledgeBaseTestClient()
    
    # 1. 搜索不存在的知识库
    print("1. 搜索不存在的知识库...")
    result = client.search_by_text("non_existent_kb", "test query")
    print(f"   结果: {result}")
    
    # 2. 上传到不存在的知识库
    print("\n2. 上传到不存在的知识库...")
    result = client.upload_json_file("non_existent_kb", SAMPLE_JSON_DATA)
    print(f"   结果: {result}")
    
    # 3. 删除不存在的知识库
    print("\n3. 删除不存在的知识库...")
    result = client.delete_knowledge_base("non_existent_kb")
    print(f"   结果: {result}")

def performance_test():
    """性能测试"""
    print("\n⚡ 性能测试")
    print("=" * 40)
    
    client = KnowledgeBaseTestClient()
    perf_kb = "performance_test_kb"
    
    # 创建测试知识库
    client.delete_knowledge_base(perf_kb)
    client.create_knowledge_base(perf_kb)
    
    # 上传文件并测试搜索性能
    print("上传测试文件...")
    start_time = time.time()
    result = client.upload_json_file(perf_kb, SAMPLE_JSON_DATA)
    upload_time = time.time() - start_time
    print(f"上传耗时: {upload_time:.2f} 秒")
    
    if result.get("status") == "success":
        time.sleep(2)  # 等待索引
        
        # 测试搜索性能
        queries = ["动物", "基金", "公司", "管理"]
        search_times = []
        
        for query in queries:
            start_time = time.time()
            client.search_by_text(perf_kb, query, top_k=5)
            search_time = time.time() - start_time
            search_times.append(search_time)
            print(f"查询 '{query}' 耗时: {search_time*1000:.1f} ms")
        
        avg_search_time = sum(search_times) / len(search_times)
        print(f"平均搜索耗时: {avg_search_time*1000:.1f} ms")
    
    # 清理
    client.delete_knowledge_base(perf_kb)

if __name__ == "__main__":
    # 运行主要测试
    run_comprehensive_test()
    
    # 运行错误测试
    test_error_cases()
    
    # 运行性能测试
    performance_test()
    
    print("\n🏁 所有测试完成!")
