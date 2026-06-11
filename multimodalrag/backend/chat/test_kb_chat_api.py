"""
对话模块测试脚本 - 精简版
包含4个核心测试：非流式、流式、重排序、历史对话
"""
import requests
import json
import os
import time

# ============ 配置 ============

CHAT_API_URL = os.getenv("CHAT_API_URL", "http://localhost:8501")
MILVUS_API_URL = os.getenv("MILVUS_API_URL", "http://localhost:8000")

TOP_K = 5  # 最终保留的文档数量

# LLM配置
LLM_CONFIG = {
    "api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
    "model_name": os.getenv("TEST_MODEL_NAME", "qwen3-max"),
    "temperature": 0.2,
    "max_tokens": 10240
}

# 重排序配置（如果不使用，留空即可）
RERANKER_CONFIG = {
    "api_url": "https://api.jina.ai/v1",
    "api_key": os.getenv("JINA_API_KEY", ""),
    "model_name": "jina-reranker-v2-base-multilingual",
    "top_n": TOP_K
}

COLLECTION_NAME = "test_knowledge_base"

# ============ 工具函数 ============

def print_separator(title=""):
    """打印分隔线"""
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}\n")
    else:
        print("-" * 60)

# ============ 测试函数 ============

def test_non_stream_chat():
    """测试1: 非流式对话（不使用重排序）"""
    print_separator("测试1: 非流式对话")
    
    data = {
        "query": "代码自解释的目标是什么?",
        "collection_name": COLLECTION_NAME,
        "llm_config": LLM_CONFIG,
        "top_k": TOP_K,  # 不使用重排序，直接召回TOP_K个
        "score_threshold": 0.3,
        "stream": False,
        "return_source": True,
        "milvus_api_url": MILVUS_API_URL
    }
    
    try:
        print(f"问题: {data['query']}")
        print(f"召回配置: TOP_K={TOP_K}（不使用重排序）")
        print("发送请求...")
        start_time = time.time()
        
        response = requests.post(
            f"{CHAT_API_URL}/chat",
            json=data,
            timeout=60
        )
        
        elapsed = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            
            print(f"\n✓ 对话成功 (总耗时: {elapsed:.2f}秒)")
            print(f"\n【回答】\n{result['answer']}\n")
            
            if result.get("sources"):
                print(f"【来源文档】 共 {len(result['sources'])} 个:")
                for i, doc in enumerate(result['sources'], 1):
                    print(f"  {i}. 片段内容：{doc['chunk_text']}")
                    print(f"     文件名: {doc['filename']}")
                    
                    # 显示分数信息
                    if doc.get('rerank_score') is not None:
                        print(f"     召回分数: {doc.get('retrieval_score', 0):.3f}")
                        print(f"     重排分数: {doc.get('rerank_score', 0):.3f} ⭐")
                    else:
                        print(f"     相关度: {doc['score']:.3f}")
            
            metadata = result.get("metadata", {})
            print(f"\n【性能指标】")
            print(f"  召回耗时: {metadata.get('retrieve_time', 0):.2f}s")
            print(f"  LLM耗时: {metadata.get('llm_time', 0):.2f}s")
            print(f"  总耗时: {metadata.get('total_time', 0):.2f}s")
            print(f"  文档数量: {metadata.get('documents_count', 0)}")
            
            return True, result['answer']
        else:
            print(f"✗ 请求失败: {response.status_code}")
            print(response.text)
            return False, None
            
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False, None

def test_stream_chat():
    """测试2: 流式对话（不使用重排序）"""
    print_separator("测试2: 流式对话")
    
    data = {
        "query": "代码自解释有哪些应用场景?",
        "collection_name": COLLECTION_NAME,
        "llm_config": LLM_CONFIG,
        "top_k": TOP_K,  # 不使用重排序，直接召回TOP_K个
        "score_threshold": 0.3,
        "stream": True,
        "return_source": True,
        "milvus_api_url": MILVUS_API_URL
    }
    
    try:
        print(f"问题: {data['query']}")
        print(f"召回配置: TOP_K={TOP_K}（不使用重排序）")
        print("发送流式请求...\n")
        start_time = time.time()
        
        response = requests.post(
            f"{CHAT_API_URL}/chat",
            json=data,
            stream=True,
            timeout=60
        )
        
        print("【回答】\n", end="", flush=True)
        
        answer = ""
        sources = None
        metadata = None
        
        for line in response.iter_lines():
            if line:
                try:
                    event = json.loads(line)
                    
                    if event["type"] == "content":
                        content = event["data"]
                        print(content, end="", flush=True)
                        answer += content
                    
                    elif event["type"] == "sources":
                        sources = event["data"]
                    
                    elif event["type"] == "metadata":
                        metadata = event["data"]
                    
                    elif event["type"] == "error":
                        print(f"\n✗ 错误: {event['data']['error']}")
                        return False, None
                        
                except json.JSONDecodeError:
                    print(f"\n⚠️ 无法解析行: {line}")
        
        elapsed = time.time() - start_time
        print(f"\n\n✓ 流式对话完成 (总耗时: {elapsed:.2f}秒)")
        
        if sources:
            print(f"\n【来源文档】 共 {len(sources)} 个:")
            for i, doc in enumerate(sources, 1):
                print(f"  {i}. 片段内容：{doc['chunk_text']}")
                print(f"     文件名: {doc['filename']}")
                
                if doc.get('rerank_score') is not None:
                    print(f"     召回分数: {doc.get('retrieval_score', 0):.3f}")
                    print(f"     重排分数: {doc.get('rerank_score', 0):.3f} ⭐")
                else:
                    print(f"     相关度: {doc['score']:.3f}")
        
        if metadata:
            print(f"\n【性能指标】")
            print(f"  召回耗时: {metadata.get('retrieve_time', 0):.2f}s")
            print(f"  LLM耗时: {metadata.get('llm_time', 0):.2f}s")
            print(f"  总耗时: {metadata.get('total_time', 0):.2f}s")
            print(f"  文档数量: {metadata.get('documents_count', 0)}")
        
        return True, answer
        
    except Exception as e:
        print(f"\n✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False, None

def test_with_reranker():
    """测试3: 使用重排序"""
    print_separator("测试3: 使用重排序")
    
    # 检查是否配置了重排序API
    if not RERANKER_CONFIG.get("api_key") or RERANKER_CONFIG["api_key"] == "":
        print("⚠️ 未配置重排序API，跳过此测试")
        print("提示: 在配置中设置 RERANKER_CONFIG['api_key'] 来启用重排序测试")
        return False, None
    
    # 使用重排序：召回TOP_K*3个文档，重排序后保留TOP_K个
    retrieve_count = TOP_K * 3
    
    data = {
        "query": "代码自解释的实现方法有哪些?",
        "collection_name": COLLECTION_NAME,
        "llm_config": LLM_CONFIG,
        "top_k": retrieve_count,  # 召回TOP_K*3个文档
        "score_threshold": 0.2,
        "use_reranker": True,
        "reranker_config": RERANKER_CONFIG,  # 重排序后保留TOP_K个
        "stream": False,
        "return_source": True,
        "milvus_api_url": MILVUS_API_URL
    }
    
    try:
        print(f"问题: {data['query']}")
        print(f"召回配置: 召回 {retrieve_count} 个文档（TOP_K×3），重排序后保留 {TOP_K} 个")
        print("发送请求...")
        start_time = time.time()
        
        response = requests.post(
            f"{CHAT_API_URL}/chat",
            json=data,
            timeout=60
        )
        
        elapsed = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            
            print(f"\n✓ 对话成功 (总耗时: {elapsed:.2f}秒)")
            print(f"\n【回答】\n{result['answer']}\n")
            
            if result.get("sources"):
                print(f"【来源文档（重排序后）】 共 {len(result['sources'])} 个:")
                for i, doc in enumerate(result['sources'], 1):
                    print(f"  {i}. 片段内容：{doc['chunk_text']}")
                    print(f"     文件名: {doc['filename']}")
                    print(f"     召回分数: {doc.get('retrieval_score', 0):.3f}")
                    print(f"     重排分数: {doc.get('rerank_score', 0):.3f} ⭐")
            
            metadata = result.get("metadata", {})
            print(f"\n【性能指标】")
            print(f"  召回耗时: {metadata.get('retrieve_time', 0):.2f}s")
            print(f"  重排序耗时: {metadata.get('rerank_time', 0):.2f}s")
            print(f"  LLM耗时: {metadata.get('llm_time', 0):.2f}s")
            print(f"  总耗时: {metadata.get('total_time', 0):.2f}s")
            print(f"  文档数量: {metadata.get('documents_count', 0)}")
            
            return True, result['answer']
        else:
            print(f"✗ 请求失败: {response.status_code}")
            print(response.text)
            return False, None
            
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False, None

def test_with_history():
    """测试4: 带历史对话记录（不使用重排序）"""
    print_separator("测试4: 带历史对话记录")
    
    # 模拟历史对话
    history = [
        {
            "role": "user",
            "content": "代码自解释是什么?"
        },
        {
            "role": "assistant",
            "content": "代码自解释是一种编程实践，强调编写清晰易懂的代码，使代码本身就能表达其意图和功能，减少对注释的依赖。"
        }
    ]
    
    # 基于历史的新问题
    current_query = "它有哪些具体的实践技巧?"
    
    data = {
        "query": current_query,
        "collection_name": COLLECTION_NAME,
        "llm_config": LLM_CONFIG,
        "history": history,  # 传入历史对话
        "top_k": TOP_K,  # 不使用重排序，直接召回TOP_K个
        "score_threshold": 0.3,
        "stream": False,
        "return_source": True,
        "milvus_api_url": MILVUS_API_URL
    }
    
    try:
        print("【历史对话】")
        for msg in history:
            role = "用户" if msg["role"] == "user" else "助手"
            print(f"{role}: {msg['content']}")
        
        print(f"\n【当前问题】")
        print(f"用户: {current_query}")
        print(f"召回配置: TOP_K={TOP_K}（不使用重排序）")
        
        print("\n发送请求（带历史记录）...")
        start_time = time.time()
        
        response = requests.post(
            f"{CHAT_API_URL}/chat",
            json=data,
            timeout=60
        )
        
        elapsed = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            
            print(f"\n✓ 对话成功 (总耗时: {elapsed:.2f}秒)")
            print(f"\n【回答】\n{result['answer']}\n")
            
            if result.get("sources"):
                print(f"【来源文档】 共 {len(result['sources'])} 个:")
                for i, doc in enumerate(result['sources'], 1):
                    print(f"  {i}. 片段内容：{doc['chunk_text']}")
                    print(f"     文件名: {doc['filename']} (相关度: {doc['score']:.3f})")
            
            metadata = result.get("metadata", {})
            print(f"\n【性能指标】")
            print(f"  召回耗时: {metadata.get('retrieve_time', 0):.2f}s")
            print(f"  LLM耗时: {metadata.get('llm_time', 0):.2f}s")
            print(f"  总耗时: {metadata.get('total_time', 0):.2f}s")
            print(f"  文档数量: {metadata.get('documents_count', 0)}")
            
            print("\n💡 提示: 模型能够理解上下文，'它'指的是之前提到的'代码自解释'")
            
            return True, result['answer']
        else:
            print(f"✗ 请求失败: {response.status_code}")
            print(response.text)
            return False, None
            
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False, None

# ============ 主测试流程 ============

def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("  RAG对话模块 - 核心功能测试")
    print("="*60)
    print(f"\n配置说明:")
    print(f"  TOP_K = {TOP_K}")
    print(f"  - 不使用重排序: 召回 {TOP_K} 个文档")
    print(f"  - 使用重排序: 召回 {TOP_K * 3} 个文档，重排序后保留 {TOP_K} 个")
    
    tests = [
        ("非流式对话", test_non_stream_chat),
        ("流式对话", test_stream_chat),
        ("使用重排序", test_with_reranker),
        ("带历史对话", test_with_history),
    ]
    
    results = []
    
    for name, test_func in tests:
        try:
            success, _ = test_func()
            results.append((name, success))
            time.sleep(1)  # 短暂延迟
        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断测试")
            break
        except Exception as e:
            print(f"\n✗ 测试异常: {e}")
            results.append((name, False))
    
    # 汇总结果
    print_separator("测试结果汇总")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{status} - {name}")
    
    print(f"\n通过: {passed}/{total}")
    
    if passed == total:
        print("\n🎉 所有测试通过!")
    else:
        print(f"\n⚠️ {total - passed} 个测试失败")

if __name__ == "__main__":
    import sys
    
    # 检查服务是否运行
    print("\n正在检查服务状态...")
    try:
        response = requests.get(f"{CHAT_API_URL}/health", timeout=5)
        if response.status_code == 200:
            print(f"✓ 服务正常运行: {CHAT_API_URL}")
        else:
            print(f"⚠️ 服务响应异常: {response.status_code}")
    except Exception as e:
        print(f"✗ 无法连接到服务: {e}")
        print(f"\n请先启动服务:")
        print(f"  cd /home/data/nongwa/workspace/chat")
        print(f"  python kb_chat.py")
        sys.exit(1)
    
    run_all_tests()
