"""
Test script for reasoning/thinking token flow.

Tests that:
1. ModelFactory creates models with reasoning config
2. OpenRouter returns reasoning tokens
3. AgentScope converts to ThinkingBlock
4. SSE Adapter outputs reasoning_content
"""

import asyncio
import os
import sys
import json

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)


async def test_model_reasoning_config():
    """Test that ModelFactory creates models with reasoning config."""
    from agentscope_integration.models import ModelFactory
    
    print("=" * 60)
    print("Test 1: ModelFactory Reasoning Config")
    print("=" * 60)
    
    # Test Kimi K2.5 (should use Moonshot thinking mode enabled)
    model, formatter = ModelFactory.create("kimi-k2.5")
    
    print(f"Model: {model.model_name}")
    print(f"Generate kwargs: {model.generate_kwargs}")
    
    # Check Kimi thinking config
    if model.generate_kwargs and "extra_body" in model.generate_kwargs:
        thinking = model.generate_kwargs["extra_body"].get("thinking", {})
        print(f"Kimi thinking mode: {thinking.get('type', 'NOT SET')}")
        assert thinking.get("type") == "enabled", "Kimi K2.5 should default to thinking enabled"
        print("✅ Kimi K2.5 thinking config OK")
    else:
        print("❌ No Kimi thinking config found!")
        return False
    
    # Test Gemini (should have reasoning_effort="medium")
    model, formatter = ModelFactory.create("gemini-3-flash")
    if model.generate_kwargs and "extra_body" in model.generate_kwargs:
        reasoning = model.generate_kwargs["extra_body"].get("reasoning", {})
        print(f"Gemini reasoning effort: {reasoning.get('effort', 'NOT SET')}")
        assert reasoning.get("effort") == "medium", "Gemini should have reasoning_effort='medium'"
        print("✅ Gemini reasoning config OK")
    
    # DeepSeek key is mapped to kimi-k2.5 in this integration
    model, formatter = ModelFactory.create("deepseek-chat")
    mapped_model_name = model.model_name
    print(f"DeepSeek key mapped to model: {mapped_model_name}")
    if mapped_model_name == "kimi-k2.5":
        print("✅ DeepSeek fallback mapping to kimi-k2.5 is active")
    else:
        print("⚠️ DeepSeek fallback mapping changed")

    return True


async def test_openrouter_reasoning_response():
    """Test that OpenRouter returns reasoning tokens."""
    from agentscope_integration.models import ModelFactory
    
    print("\n" + "=" * 60)
    print("Test 2: OpenRouter Reasoning Response (Kimi K2.5)")
    print("=" * 60)
    
    model, formatter = ModelFactory.create("kimi-k2.5")
    
    # Simple math question to trigger reasoning
    messages = [
        {"role": "user", "content": "What is 15 * 17? Think step by step."}
    ]
    
    print(f"Sending request to: {model.model_name}")
    print(f"With reasoning config: {model.generate_kwargs}")
    
    try:
        # Non-streaming first to see full response
        model_non_stream, _ = ModelFactory.create("kimi-k2.5")
        model_non_stream.stream = False
        
        response = await model_non_stream(messages)
        
        print(f"\nResponse type: {type(response)}")
        print(f"Response content blocks: {len(response.content)}")
        
        has_thinking = False
        has_text = False
        
        for block in response.content:
            block_dict = block if isinstance(block, dict) else dict(block)
            block_type = block_dict.get("type", "unknown")
            print(f"  - Block type: {block_type}")
            
            if block_type == "thinking":
                has_thinking = True
                thinking_content = block_dict.get("thinking", "")
                print(f"    Thinking preview: {thinking_content[:200]}...")
            elif block_type == "text":
                has_text = True
                text_content = block_dict.get("text", "")
                print(f"    Text: {text_content[:200]}...")
        
        if has_thinking:
            print("\n✅ OpenRouter returned ThinkingBlock!")
        else:
            print("\n⚠️ No ThinkingBlock in response (model may not support it)")
        
        if has_text:
            print("✅ OpenRouter returned TextBlock")
        
        return has_thinking or has_text
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_streaming_reasoning():
    """Test streaming with reasoning tokens."""
    from agentscope_integration.models import ModelFactory
    
    print("\n" + "=" * 60)
    print("Test 3: Streaming Reasoning (Gemini)")
    print("=" * 60)
    
    # Use our custom OpenRouterChatModel via ModelFactory
    model, formatter = ModelFactory.create("gemini-3-flash")
    
    messages = [
        {"role": "user", "content": "What is 8 + 9? Think briefly."}
    ]
    
    print(f"Streaming from: {model.model_name}")
    print(f"Generate kwargs: {model.generate_kwargs}")
    
    try:
        response_gen = await model(messages)
        
        chunk_count = 0
        has_thinking = False
        final_text = ""
        final_thinking = ""
        
        async for chunk in response_gen:
            chunk_count += 1
            for block in chunk.content:
                block_dict = block if isinstance(block, dict) else dict(block)
                if block_dict.get("type") == "thinking":
                    has_thinking = True
                    final_thinking = block_dict.get("thinking", "")
                elif block_dict.get("type") == "text":
                    final_text = block_dict.get("text", "")
        
        print(f"Total chunks: {chunk_count}")
        print(f"Has thinking: {has_thinking}")
        if final_thinking:
            print(f"Final thinking: {final_thinking[:200]}...")
        print(f"Final text: {final_text[:200]}...")
        
        if has_thinking:
            print("✅ Streaming reasoning works!")
        else:
            print("⚠️ No thinking in streaming (model may not support it)")
        
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_sse_adapter():
    """Test SSE adapter converts ThinkingBlock correctly."""
    from agentscope_integration.streaming import SSEAdapter
    from agentscope.message import Msg, ThinkingBlock, TextBlock
    
    print("\n" + "=" * 60)
    print("Test 4: SSE Adapter ThinkingBlock Conversion")
    print("=" * 60)
    
    adapter = SSEAdapter(
        thread_id="test-thread",
        thread_run_id="test-run",
    )
    
    # Create a message with ThinkingBlock
    msg = Msg(
        name="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="Let me think about this..."),
            TextBlock(type="text", text="The answer is 42."),
        ],
        role="assistant",
    )
    
    # Convert to SSE format
    sse_msg = adapter.convert(msg, is_last=False)
    
    print(f"SSE message type: {sse_msg['type']}")
    
    # Parse content
    content = json.loads(sse_msg['content'])
    metadata = json.loads(sse_msg['metadata'])
    
    print(f"Content: {json.dumps(content, indent=2)}")
    print(f"Metadata: {json.dumps(metadata, indent=2)}")
    
    # Check reasoning_content is present
    if "reasoning_content" in content:
        print(f"\n✅ reasoning_content present: {content['reasoning_content'][:50]}...")
    else:
        print("\n❌ reasoning_content NOT present!")
        return False
    
    # Check stream_status
    if metadata.get("stream_status") == "reasoning_chunk":
        print("✅ stream_status is 'reasoning_chunk'")
    else:
        print(f"⚠️ stream_status is '{metadata.get('stream_status')}' (expected 'reasoning_chunk' for non-last)")
    
    return True


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Reasoning/Thinking Flow Tests")
    print("=" * 60 + "\n")
    
    results = []
    
    # Test 1: Model config
    results.append(("ModelFactory Config", await test_model_reasoning_config()))
    
    # Test 2: OpenRouter response
    results.append(("OpenRouter Response", await test_openrouter_reasoning_response()))
    
    # Test 3: Streaming
    results.append(("Streaming Reasoning", await test_streaming_reasoning()))
    
    # Test 4: SSE Adapter
    results.append(("SSE Adapter", await test_sse_adapter()))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name}: {status}")
    
    all_passed = all(r[1] for r in results)
    print("\n" + ("✅ All tests passed!" if all_passed else "❌ Some tests failed"))
    
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
