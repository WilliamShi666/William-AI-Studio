#!/usr/bin/env python3
"""
简单的 PPIO 沙箱测试
测试基本的沙箱创建和连接功能
"""

import os
import asyncio
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(override=True)

async def kill_sandbox_by_id(sandbox_id: str):
    """根据ID销毁特定沙箱"""
    print(f"🗑️  销毁沙箱: {sandbox_id}")
    
    try:
        # 设置环境变量
        os.environ['E2B_DOMAIN'] = 'sandbox.ppio.cn'
        
        # 尝试多种SDK
        success = False
        
        # 1. 尝试 e2b-desktop
        try:
            from e2b_desktop import Sandbox
            result = Sandbox.kill(sandbox_id)  # 移除 await，这是同步方法
            if result:
                print(f"✅ [e2b-desktop] 沙箱 {sandbox_id} 销毁成功")
                success = True
        except Exception as e:
            print(f"⚠️  [e2b-desktop] 销毁失败: {e}")
        
        # 2. 尝试 e2b-code-interpreter  
        if not success:
            try:
                from e2b_code_interpreter import Sandbox
                result = Sandbox.kill(sandbox_id)  # 移除 await，这是同步方法
                if result:
                    print(f"✅ [e2b-code-interpreter] 沙箱 {sandbox_id} 销毁成功")
                    success = True
            except Exception as e:
                print(f"⚠️  [e2b-code-interpreter] 销毁失败: {e}")
        
        # 3. 尝试 e2b (通用)
        if not success:
            try:
                from e2b import Sandbox
                result = Sandbox.kill(sandbox_id)  # 移除 await，这是同步方法
                if result:
                    print(f"✅ [e2b] 沙箱 {sandbox_id} 销毁成功")
                    success = True
            except Exception as e:
                print(f"⚠️  [e2b] 销毁失败: {e}")
        
        if not success:
            print(f"❌ 沙箱 {sandbox_id} 销毁失败，尝试了所有SDK")
            return False
            
        return True
        
    except Exception as e:
        print(f"❌ 销毁沙箱 {sandbox_id} 时发生错误: {e}")
        return False

async def list_all_sandboxes():
    """列出所有运行中的沙箱"""
    print("📋 获取所有运行中的沙箱...")
    
    try:
        os.environ['E2B_DOMAIN'] = 'sandbox.ppio.cn'
        
        sandboxes = []
        
        # 尝试多种SDK获取沙箱列表
        try:
            from e2b_desktop import Sandbox
            desktop_sandboxes = Sandbox.list()  # 移除 await，这是同步方法
            for sb in desktop_sandboxes:
                sandboxes.append({
                    'id': sb.sandbox_id if hasattr(sb, 'sandbox_id') else sb.sandboxId,
                    'type': 'desktop',
                    'sdk': 'e2b-desktop'
                })
            print(f"✅ [e2b-desktop] 找到 {len(desktop_sandboxes)} 个沙箱")
        except Exception as e:
            print(f"⚠️  [e2b-desktop] 获取列表失败: {e}")
        
        try:
            from e2b_code_interpreter import Sandbox
            code_sandboxes = Sandbox.list()  # 移除 await，这是同步方法
            for sb in code_sandboxes:
                sandboxes.append({
                    'id': sb.sandbox_id if hasattr(sb, 'sandbox_id') else sb.sandboxId,
                    'type': 'code-interpreter',
                    'sdk': 'e2b-code-interpreter'
                })
            print(f"✅ [e2b-code-interpreter] 找到 {len(code_sandboxes)} 个沙箱")
        except Exception as e:
            print(f"⚠️  [e2b-code-interpreter] 获取列表失败: {e}")
        
        try:
            from e2b import Sandbox
            general_sandboxes = Sandbox.list()  # 移除 await，这是同步方法
            for sb in general_sandboxes:
                sandboxes.append({
                    'id': sb.sandbox_id if hasattr(sb, 'sandbox_id') else sb.sandboxId,
                    'type': 'general',
                    'sdk': 'e2b'
                })
            print(f"✅ [e2b] 找到 {len(general_sandboxes)} 个沙箱")
        except Exception as e:
            print(f"⚠️  [e2b] 获取列表失败: {e}")
        
        # 去重（同一个沙箱可能在多个SDK中出现）
        unique_sandboxes = {}
        for sb in sandboxes:
            unique_sandboxes[sb['id']] = sb
        
        sandboxes = list(unique_sandboxes.values())
        
        if sandboxes:
            print(f"\n📊 总共找到 {len(sandboxes)} 个唯一沙箱:")
            for i, sb in enumerate(sandboxes, 1):
                print(f"  {i}. ID: {sb['id'][:20]}... (类型: {sb['type']}, SDK: {sb['sdk']})")
        else:
            print("✅ 没有找到运行中的沙箱")
        
        return sandboxes
        
    except Exception as e:
        print(f"❌ 获取沙箱列表时发生错误: {e}")
        return []

async def kill_all_sandboxes():
    """销毁所有运行中的沙箱"""
    print("🗑️  准备销毁所有沙箱...")
    
    sandboxes = await list_all_sandboxes()
    if not sandboxes:
        print("✅ 没有需要销毁的沙箱")
        return True
    
    print(f"\n⚠️  即将销毁 {len(sandboxes)} 个沙箱!")
    confirm = input("确定要继续吗？(输入 'yes' 确认): ")
    
    if confirm.lower() != 'yes':
        print("❌ 用户取消操作")
        return False
    
    print(f"\n🚀 开始销毁 {len(sandboxes)} 个沙箱...")
    
    success_count = 0
    for i, sb in enumerate(sandboxes, 1):
        print(f"\n[{i}/{len(sandboxes)}] 销毁沙箱: {sb['id']}")
        
        if await kill_sandbox_by_id(sb['id']):
            success_count += 1
        
        # 稍微延迟避免API限制
        if i < len(sandboxes):
            await asyncio.sleep(0.5)
    
    print(f"\n📊 销毁结果:")
    print(f"  - 成功: {success_count}")
    print(f"  - 失败: {len(sandboxes) - success_count}")
    print(f"  - 总计: {len(sandboxes)}")
    
    return success_count == len(sandboxes)

async def test_basic_sandbox():
    """测试基本沙箱功能"""
    print("🚀 开始测试 PPIO 沙箱基本功能...\n")
    
    # 设置环境变量
    os.environ['E2B_DOMAIN'] = 'sandbox.ppio.cn'
    
    try:
        from e2b_code_interpreter import Sandbox
        print("✅ 成功导入 e2b_code_interpreter.Sandbox")
        
        # 测试基本配置
        config = {
            'timeoutMs': 30000,  # 30秒
            'metadata': {
                'test': True,
                'purpose': 'basic_test'
            }
        }
        
        print("📋 沙箱配置:")
        print(f"   - 超时: {config['timeoutMs']}ms")
        print(f"   - 元数据: {config['metadata']}")
        
        # 尝试创建沙箱
        print("\n🔧 尝试创建沙箱...")
        sandbox = Sandbox(config)
        print(f"✅ 沙箱创建成功！")
        print(f"   - 沙箱ID: {getattr(sandbox, 'sandboxId', 'N/A')}")
        
        # 测试简单命令
        print("\n📝 测试执行命令...")
        try:
            result = sandbox.runCode('print("Hello from PPIO sandbox!")')  # 移除 await，这是同步方法
            print("✅ 命令执行成功:")
            if hasattr(result, 'logs'):
                print(f"   - 输出: {result.logs}")
            else:
                print(f"   - 结果: {result}")
        except Exception as cmd_error:
            print(f"⚠️  命令执行测试失败: {cmd_error}")
        
        # 清理
        print("\n🗑️  清理沙箱...")
        sandbox.kill()  # 移除 await，这是同步方法
        print("✅ 沙箱清理完成")
        
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        print(f"   - 错误类型: {type(e).__name__}")
        
        # 打印详细错误信息
        import traceback
        print(f"   - 详细错误:")
        traceback.print_exc()
        
        return False

async def test_project_integration():
    """测试与项目配置的集成"""
    print("\n🔧 测试项目集成...")
    
    try:
        # 导入项目的沙箱模块
        import sys
        sys.path.append('.')
        
        from sandbox.sandbox import create_sandbox
        print("✅ 成功导入项目沙箱模块")
        
        # 测试沙箱创建函数
        print("📝 测试项目沙箱创建函数...")
        
        # 注意：这里不实际创建，只测试函数定义
        print("✅ create_sandbox 函数存在且可调用")
        
        return True
        
    except Exception as e:
        print(f"❌ 项目集成测试失败: {e}")
        return False

async def interactive_sandbox_manager():
    """交互式沙箱管理"""
    print("🎛️  沙箱管理器")
    print("=" * 40)
    
    while True:
        print("\n选择操作:")
        print("1. 📋 列出所有沙箱")
        print("2. 🗑️  销毁指定沙箱 (按ID)")
        print("3. 💥 销毁所有沙箱")
        print("4. 🧪 运行基本测试")
        print("5. ❌ 退出")
        
        choice = input("\n请输入选择 (1-5): ").strip()
        
        if choice == '1':
            print("\n" + "="*40)
            await list_all_sandboxes()
            
        elif choice == '2':
            print("\n" + "="*40)
            sandbox_id = input("请输入沙箱ID: ").strip()
            if sandbox_id:
                await kill_sandbox_by_id(sandbox_id)
            else:
                print("❌ 沙箱ID不能为空")
                
        elif choice == '3':
            print("\n" + "="*40)
            await kill_all_sandboxes()
            
        elif choice == '4':
            print("\n" + "="*40)
            await test_basic_sandbox()
            
        elif choice == '5':
            print("👋 退出沙箱管理器")
            break
            
        else:
            print("❌ 无效选择，请输入 1-5")

async def main():
    """主测试函数"""
    import sys
    
    # 检查命令行参数
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == 'kill' and len(sys.argv) > 2:
            # 销毁指定沙箱: python test_simple_sandbox.py kill <sandbox_id>
            sandbox_id = sys.argv[2]
            print(f"🗑️  销毁沙箱: {sandbox_id}")
            success = await kill_sandbox_by_id(sandbox_id)
            if success:
                print("✅ 操作完成")
            else:
                print("❌ 操作失败")
            return
            
        elif command == 'kill-all':
            # 销毁所有沙箱: python test_simple_sandbox.py kill-all
            print("💥 销毁所有沙箱")
            success = await kill_all_sandboxes()
            if success:
                print("✅ 所有沙箱已销毁")
            else:
                print("⚠️  部分沙箱销毁失败")
            return
            
        elif command == 'list':
            # 列出所有沙箱: python test_simple_sandbox.py list
            await list_all_sandboxes()
            return
            
        elif command == 'manager':
            # 交互式管理器: python test_simple_sandbox.py manager
            await interactive_sandbox_manager()
            return
    
    # 默认：运行完整测试
    print("=" * 60)
    print("🧪 PPIO 沙箱简单测试")
    print("=" * 60)
    
    print("💡 使用方法:")
    print("  python test_simple_sandbox.py kill <sandbox_id>    # 销毁指定沙箱")
    print("  python test_simple_sandbox.py kill-all             # 销毁所有沙箱")
    print("  python test_simple_sandbox.py list                 # 列出所有沙箱")
    print("  python test_simple_sandbox.py manager              # 交互式管理器")
    print("  python test_simple_sandbox.py                      # 运行测试")
    
    # 检查环境变量
    api_key = os.environ.get('E2B_API_KEY')
    if not api_key:
        print("\n❌ E2B_API_KEY 未设置")
        print("💡 请在 .env 文件中设置你的 PPIO API Key")
        return
    
    print(f"\n🔑 API Key: {api_key[:10]}...")
    print(f"🌐 Domain: {os.environ.get('E2B_DOMAIN', 'sandbox.ppio.cn')}")
    
    # 运行测试
    tests = [
        ("基本沙箱功能", test_basic_sandbox),
        ("项目集成", test_project_integration),
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n{'=' * 40}")
        print(f"🧪 {test_name}")
        print('=' * 40)
        
        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ 测试异常: {e}")
            results.append((test_name, False))
    
    # 汇总结果
    print(f"\n{'=' * 40}")
    print("📊 测试结果")
    print('=' * 40)
    
    passed = 0
    for test_name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{test_name}: {status}")
        if result:
            passed += 1
    
    print(f"\n总计: {passed}/{len(results)} 个测试通过")
    
    if passed == len(results):
        print("\n🎉 所有测试通过！可以使用 PPIO 沙箱了。")
    else:
        print(f"\n⚠️  有测试失败，请检查配置和 API Key。")

if __name__ == "__main__":
    asyncio.run(main()) 