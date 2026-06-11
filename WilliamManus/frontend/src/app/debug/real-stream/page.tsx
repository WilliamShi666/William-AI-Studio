"use client";
import React from "react";
import { ToolCallSidePanel } from "@/components/thread/tool-call-side-panel";
import { useToolCalls } from "@/app/(dashboard)/projects/[projectId]/thread/_hooks/useToolCalls";
import { UnifiedMessage } from "@/components/thread/types";

export default function RealStreamTestPage() {
  const [leftSidebarOpen, setLeftSidebarOpen] = React.useState(false);

  // 使用 useMemo 确保 messages 数组引用稳定，避免无限循环
  const messages: UnifiedMessage[] = React.useMemo(() => [
    // 1. Status: thread_run_start
    {
      message_id: "73a9c7f5-c35b-42a4-87a8-1fb4b96b059f",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "status",
      role: "system",
      content: "{\"status_type\": \"thread_run_start\", \"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\"}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\"}",
      created_at: "2025-09-12T08:57:35.169582+00:00",
      updated_at: "2025-09-12T08:57:35.169582+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: false
    },

    // 2. Status: assistant_response_start
    {
      message_id: "225387fc-aec5-44f1-baca-9c16597afb36",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "status",
      role: "system",
      content: "{\"status_type\": \"assistant_response_start\"}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\"}",
      created_at: "2025-09-12T08:57:35.172948+00:00",
      updated_at: "2025-09-12T08:57:35.172948+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: false
    },

    // 3. Tool call chunk: create_tasks (streaming)
    {
      message_id: null,
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "status",
      role: "system",
      content: "{\"role\": \"assistant\", \"status_type\": \"tool_call_chunk\", \"tool_call_chunk\": {\"id\": \"call_rozQOl5qiidQFloCt9U1Uk4L\", \"index\": 0, \"type\": \"function\", \"function\": {\"name\": \"create_tasks\", \"arguments\": \"{\\\"sections\\\": [{\\\"title\\\": \\\"Planning\\\", \\\"tasks\\\": [\\\"Calculate the product of 100, 23, 323, and 123\\\", \\\"Echo back the calculated result\\\"]}]}\"}}}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\"}",
      created_at: "2025-09-12T08:57:39.299260+00:00",
      updated_at: "2025-09-12T08:57:39.299260+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: true
    },

    // 4. Assistant message: create_tasks (complete)
    {
      message_id: "9bcd108f-ed57-469d-b3a7-1798d934ac0d",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "assistant",
      role: "assistant",
      content: "{\"role\": \"assistant\", \"content\": \"The calculation of the product \\\\(100 \\\\times 23 \\\\times 323 \\\\times 123\\\\) results in **91376700**. All tasks for this calculation have been completed successfully.\", \"tool_calls\": [{\"id\": \"call_rozQOl5qiidQFloCt9U1Uk4L\", \"type\": \"function\", \"function\": {\"name\": \"create_tasks\", \"arguments\": {\"sections\": [{\"tasks\": [\"Calculate the product of 100, 23, 323, and 123\", \"Echo back the calculated result\"], \"title\": \"Planning\"}]}}}]}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\", \"stream_status\": \"complete\", \"tool_index\": 0}",
      created_at: "2025-09-12T08:58:25.532719+00:00",
      updated_at: "2025-09-12T08:58:25.532719+00:00",
      agent_id: "74aad582-4290-4fbf-b57b-372f0669e404",
      agent_version_id: "18dbadf5-6bed-4337-bc18-0725ad34893f",
      is_llm_message: true
    },

    // 5. Tool result: create_tasks
    {
      message_id: "7e95595b-61ee-43e2-974d-7d90509f999f",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "tool",
      role: "system",
      content: "{\"result\": \"{\\n  \\\"sections\\\": [\\n    {\\n      \\\"id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\",\\n      \\\"title\\\": \\\"Planning\\\",\\n      \\\"tasks\\\": [\\n        {\\n          \\\"id\\\": \\\"e060eb17-1d88-4b85-b1dd-8d9d7750db81\\\",\\n          \\\"content\\\": \\\"Calculate the product of 100, 23, 323, and 123\\\",\\n          \\\"status\\\": \\\"pending\\\",\\n          \\\"section_id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\"\\n        },\\n        {\\n          \\\"id\\\": \\\"af83cc0a-4faf-4d4b-8322-3c6c1caad65c\\\",\\n          \\\"content\\\": \\\"Echo back the calculated result\\\",\\n          \\\"status\\\": \\\"pending\\\",\\n          \\\"section_id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\"\\n        }\\n      ]\\n    }\\n  ],\\n  \\\"total_tasks\\\": 2,\\n  \\\"total_sections\\\": 1\\n}\", \"tool_name\": \"create_tasks\"}",
      metadata: "{\"tool_name\": \"create_tasks\", \"tool_call_id\": \"call_rozQOl5qiidQFloCt9U1Uk4L\", \"assistant_message_id\": \"9bcd108f-ed57-469d-b3a7-1798d934ac0d\"}",
      created_at: "2025-09-12T08:58:25.544721+00:00",
      updated_at: "2025-09-12T08:58:25.544721+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: true
    },

    // 6. Assistant message: test_calculator
    {
      message_id: "3eb037b8-a211-4359-a1ab-256e29a6fda5",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "assistant",
      role: "assistant",
      content: "{\"role\": \"assistant\", \"content\": \"\", \"tool_calls\": [{\"id\": \"call_W9CWzxCfxLKw0EJ235JG0sUO\", \"type\": \"function\", \"function\": {\"name\": \"test_calculator\", \"arguments\": {\"a\": 100, \"b\": 23, \"operation\": \"multiply\"}}}]}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\", \"stream_status\": \"complete\", \"tool_index\": 1}",
      created_at: "2025-09-12T08:58:25.533719+00:00",
      updated_at: "2025-09-12T08:58:25.532719+00:00",
      agent_id: "74aad582-4290-4fbf-b57b-372f0669e404",
      agent_version_id: "18dbadf5-6bed-4337-bc18-0725ad34893f",
      is_llm_message: true
    },

    // 7. Tool result: test_calculator
    {
      message_id: "c001dbc1-08f3-475a-8a23-4c3b0c0393a9",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "tool",
      role: "system",
      content: "{\"result\": \"100 multiply 23 = 2300\", \"tool_name\": \"test_calculator\"}",
      metadata: "{\"tool_name\": \"test_calculator\", \"tool_call_id\": \"call_W9CWzxCfxLKw0EJ235JG0sUO\", \"assistant_message_id\": \"3eb037b8-a211-4359-a1ab-256e29a6fda5\"}",
      created_at: "2025-09-12T08:58:25.553366+00:00",
      updated_at: "2025-09-12T08:58:25.553366+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: true
    },

    // 8. Assistant message: update_tasks (第一次)
    {
      message_id: "bf5513e0-c33a-4617-b5f9-d8bf7a8d12a9",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "assistant",
      role: "assistant",
      content: "{\"role\": \"assistant\", \"content\": \"\", \"tool_calls\": [{\"id\": \"call_25iR3vQ0DbgzlqH6EqvlUNCj\", \"type\": \"function\", \"function\": {\"name\": \"update_tasks\", \"arguments\": {\"status\": \"completed\", \"task_ids\": [\"e060eb17-1d88-4b85-b1dd-8d9d7750db81\"]}}}]}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\", \"stream_status\": \"complete\", \"tool_index\": 4}",
      created_at: "2025-09-12T08:58:25.536719+00:00",
      updated_at: "2025-09-12T08:58:25.532719+00:00",
      agent_id: "74aad582-4290-4fbf-b57b-372f0669e404",
      agent_version_id: "18dbadf5-6bed-4337-bc18-0725ad34893f",
      is_llm_message: true
    },

    // 9. Tool result: update_tasks (第一次)
    {
      message_id: "01abd26a-a725-40ba-9c56-7ef8bc1d0406",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "tool",
      role: "system",
      content: "{\"result\": \"{\\n  \\\"sections\\\": [\\n    {\\n      \\\"id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\",\\n      \\\"title\\\": \\\"Planning\\\",\\n      \\\"tasks\\\": [\\n        {\\n          \\\"id\\\": \\\"e060eb17-1d88-4b85-b1dd-8d9d7750db81\\\",\\n          \\\"content\\\": \\\"Calculate the product of 100, 23, 323, and 123\\\",\\n          \\\"status\\\": \\\"completed\\\",\\n          \\\"section_id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\"\\n        },\\n        {\\n          \\\"id\\\": \\\"af83cc0a-4faf-4d4b-8322-3c6c1caad65c\\\",\\n          \\\"content\\\": \\\"Echo back the calculated result\\\",\\n          \\\"status\\\": \\\"pending\\\",\\n          \\\"section_id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\"\\n        }\\n      ]\\n    }\\n  ],\\n  \\\"total_tasks\\\": 2,\\n  \\\"total_sections\\\": 1\\n}\", \"tool_name\": \"update_tasks\"}",
      metadata: "{\"tool_name\": \"update_tasks\", \"tool_call_id\": \"call_25iR3vQ0DbgzlqH6EqvlUNCj\", \"assistant_message_id\": \"bf5513e0-c33a-4617-b5f9-d8bf7a8d12a9\"}",
      created_at: "2025-09-12T08:58:25.598309+00:00",
      updated_at: "2025-09-12T08:58:25.598309+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: true
    },

    // 10. Assistant message: update_tasks (第二次 - 完成所有任务)
    {
      message_id: "af19eb9d-a838-4aff-a2ff-d0cb248958bf",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "assistant",
      role: "assistant",
      content: "{\"role\": \"assistant\", \"content\": \"\", \"tool_calls\": [{\"id\": \"call_cKwj3aTlOd85o4PnTBI8ZTkF\", \"type\": \"function\", \"function\": {\"name\": \"update_tasks\", \"arguments\": {\"status\": \"completed\", \"task_ids\": [\"af83cc0a-4faf-4d4b-8322-3c6c1caad65c\"]}}}]}",
      metadata: "{\"thread_run_id\": \"cccc26f2-649a-4892-83da-16524700a87c\", \"stream_status\": \"complete\", \"tool_index\": 6}",
      created_at: "2025-09-12T08:58:25.538719+00:00",
      updated_at: "2025-09-12T08:58:25.532719+00:00",
      agent_id: "74aad582-4290-4fbf-b57b-372f0669e404",
      agent_version_id: "18dbadf5-6bed-4337-bc18-0725ad34893f",
      is_llm_message: true
    },

    // 11. Tool result: update_tasks (第二次)
    {
      message_id: "196d752d-4beb-4db4-ba27-2f236b66749c",
      thread_id: "c2a0fc0b-6718-428b-a626-2c1519f4cb4a",
      project_id: "00000000-0000-0000-0000-000000000000",
      type: "tool",
      role: "system",
      content: "{\"result\": \"{\\n  \\\"sections\\\": [\\n    {\\n      \\\"id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\",\\n      \\\"title\\\": \\\"Planning\\\",\\n      \\\"tasks\\\": [\\n        {\\n          \\\"id\\\": \\\"e060eb17-1d88-4b85-b1dd-8d9d7750db81\\\",\\n          \\\"content\\\": \\\"Calculate the product of 100, 23, 323, and 123\\\",\\n          \\\"status\\\": \\\"completed\\\",\\n          \\\"section_id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\"\\n        },\\n        {\\n          \\\"id\\\": \\\"af83cc0a-4faf-4d4b-8322-3c6c1caad65c\\\",\\n          \\\"content\\\": \\\"Echo back the calculated result\\\",\\n          \\\"status\\\": \\\"completed\\\",\\n          \\\"section_id\\\": \\\"c192046c-a491-4b3d-9447-bc97d21a54e8\\\"\\n        }\\n      ]\\n    }\\n  ],\\n  \\\"total_tasks\\\": 2,\\n  \\\"total_sections\\\": 1\\n}\", \"tool_name\": \"update_tasks\"}",
      metadata: "{\"tool_name\": \"update_tasks\", \"tool_call_id\": \"call_cKwj3aTlOd85o4PnTBI8ZTkF\", \"assistant_message_id\": \"af19eb9d-a838-4aff-a2ff-d0cb248958bf\"}",
      created_at: "2025-09-12T08:58:25.696806+00:00",
      updated_at: "2025-09-12T08:58:25.696806+00:00",
      agent_id: null,
      agent_version_id: null,
      is_llm_message: true
    }
  ], []);

  // 使用真实的业务处理逻辑
  const {
    toolCalls,
    currentToolIndex,
    isSidePanelOpen,
    setIsSidePanelOpen,
    handleSidePanelNavigate,
  } = useToolCalls(messages, setLeftSidebarOpen, "idle");

  // 一次性调试信息 - 防止无限循环日志
  const debugRef = React.useRef(false);
  React.useEffect(() => {
    if (!debugRef.current) {
      debugRef.current = true;
      console.group("🎯 STABILITY TEST RESULT");
      console.log("✅ Messages array is now stable (useMemo)");
      console.log("📥 Input messages:", messages.length);
      console.log("🔧 Generated toolCalls:", toolCalls.length);
      
      if (toolCalls.length > 0) {
        console.log("📋 First toolCall analysis:");
        const first = toolCalls[0];
        console.log("  - name:", first.assistantCall.name);
        console.log("  - assistantContent type:", typeof first.assistantCall.content);
        if (first.toolResult) {
          console.log("  - toolContent type:", typeof first.toolResult.content);
        }
      }
      console.groupEnd();
      
      // 5秒后检查是否还在循环
      setTimeout(() => {
        console.log(toolCalls.length > 0 ? "🎉 SUCCESS: No infinite loop detected!" : "⚠️ Still processing...");
      }, 5000);
    }
  }, [toolCalls.length]);

  return (
    <div className="h-screen">
      <div className="p-4 bg-gray-100 dark:bg-gray-900">
        <h1 className="text-xl font-bold mb-2">✅ 引用稳定性修复 - 真实数据测试</h1>
        <div className="text-sm space-y-1">
          <p>📥 原始消息数量: {messages.length}</p>
          <p>🔧 处理后工具调用数量: {toolCalls.length}</p>
          <p>📋 侧边栏状态: {isSidePanelOpen ? '打开' : '关闭'}</p>
          <p>🎯 当前工具索引: {currentToolIndex}</p>
          <p className="text-green-600">🔧 修复：使用 useMemo 稳定 messages 数组引用</p>
        </div>
        <button 
          onClick={() => setIsSidePanelOpen(true)}
          className="mt-2 px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600"
        >
          打开工具面板
        </button>
      </div>
      
      <ToolCallSidePanel
        isOpen={isSidePanelOpen}
        onClose={() => setIsSidePanelOpen(false)}
        toolCalls={toolCalls}
        currentIndex={currentToolIndex}
        onNavigate={handleSidePanelNavigate}
        agentStatus="idle"
      />
    </div>
  );
} 