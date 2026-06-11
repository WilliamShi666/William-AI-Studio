"use client";
import React from "react";
import { ToolCallSidePanel, type ToolCallInput } from "@/components/thread/tool-call-side-panel";

export default function RealDataTestPage() {
  const [open, setOpen] = React.useState(true);

  // 基于真实数据流构建的测试用例
  const toolCalls: ToolCallInput[] = [
    // 1) create_tasks - 来自 tool_call_chunk
    {
      assistantCall: {
        name: "create_tasks",
        timestamp: "2025-09-12T08:57:39.299260+00:00",
        content: JSON.stringify({ role: "assistant", content: "Create task list for calculation." }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.544721+00:00",
        isSuccess: true,
        content: JSON.stringify({
          sections: [
            {
              id: "c192046c-a491-4b3d-9447-bc97d21a54e8",
              title: "Planning",
              tasks: [
                {
                  id: "e060eb17-1d88-4b85-b1dd-8d9d7750db81",
                  content: "Calculate the product of 100, 23, 323, and 123",
                  status: "pending",
                  section_id: "c192046c-a491-4b3d-9447-bc97d21a54e8"
                },
                {
                  id: "af83cc0a-4faf-4d4b-8322-3c6c1caad65c",
                  content: "Echo back the calculated result",
                  status: "pending",
                  section_id: "c192046c-a491-4b3d-9447-bc97d21a54e8"
                }
              ]
            }
          ],
          total_tasks: 2,
          total_sections: 1,
          tool_name: "create_tasks"
        }),
      },
    },

    // 2) test_calculator (100 * 23)
    {
      assistantCall: {
        name: "test_calculator",
        timestamp: "2025-09-12T08:57:42.135522+00:00",
        content: JSON.stringify({ role: "assistant", content: "Calculate 100 * 23" }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.553366+00:00",
        isSuccess: true,
        content: JSON.stringify({
          result: "100 multiply 23 = 2300",
          tool_name: "test_calculator"
        }),
      },
    },

    // 3) test_calculator (2300 * 323)
    {
      assistantCall: {
        name: "test_calculator",
        timestamp: "2025-09-12T08:57:53.842363+00:00",
        content: JSON.stringify({ role: "assistant", content: "Calculate 2300 * 323" }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.561379+00:00",
        isSuccess: true,
        content: JSON.stringify({
          result: "2300 multiply 323 = 742900",
          tool_name: "test_calculator"
        }),
      },
    },

    // 4) test_calculator (742900 * 123)
    {
      assistantCall: {
        name: "test_calculator",
        timestamp: "2025-09-12T08:58:06.569797+00:00",
        content: JSON.stringify({ role: "assistant", content: "Calculate 742900 * 123" }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.573700+00:00",
        isSuccess: true,
        content: JSON.stringify({
          result: "742900 multiply 123 = 91376700",
          tool_name: "test_calculator"
        }),
      },
    },

    // 5) update_tasks (完成第一个任务)
    {
      assistantCall: {
        name: "update_tasks",
        timestamp: "2025-09-12T08:58:20.644742+00:00",
        content: JSON.stringify({ role: "assistant", content: "Mark first task as completed." }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.598309+00:00",
        isSuccess: true,
        content: JSON.stringify({
          sections: [
            {
              id: "c192046c-a491-4b3d-9447-bc97d21a54e8",
              title: "Planning",
              tasks: [
                {
                  id: "e060eb17-1d88-4b85-b1dd-8d9d7750db81",
                  content: "Calculate the product of 100, 23, 323, and 123",
                  status: "completed",
                  section_id: "c192046c-a491-4b3d-9447-bc97d21a54e8"
                },
                {
                  id: "af83cc0a-4faf-4d4b-8322-3c6c1caad65c",
                  content: "Echo back the calculated result",
                  status: "pending",
                  section_id: "c192046c-a491-4b3d-9447-bc97d21a54e8"
                }
              ]
            }
          ],
          total_tasks: 2,
          total_sections: 1,
          tool_name: "update_tasks"
        }),
      },
    },

    // 6) test_echo
    {
      assistantCall: {
        name: "test_echo",
        timestamp: "2025-09-12T08:58:20.650126+00:00",
        content: JSON.stringify({ role: "assistant", content: "Echo result: 91376700" }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.668158+00:00",
        isSuccess: true,
        content: JSON.stringify({
          result: "91376700",
          tool_name: "test_echo"
        }),
      },
    },

    // 7) update_tasks (完成第二个任务)
    {
      assistantCall: {
        name: "update_tasks",
        timestamp: "2025-09-12T08:58:22.551546+00:00",
        content: JSON.stringify({ role: "assistant", content: "Mark second task as completed." }),
      },
      toolResult: {
        timestamp: "2025-09-12T08:58:25.696806+00:00",
        isSuccess: true,
        content: JSON.stringify({
          sections: [
            {
              id: "c192046c-a491-4b3d-9447-bc97d21a54e8",
              title: "Planning",
              tasks: [
                {
                  id: "e060eb17-1d88-4b85-b1dd-8d9d7750db81",
                  content: "Calculate the product of 100, 23, 323, and 123",
                  status: "completed",
                  section_id: "c192046c-a491-4b3d-9447-bc97d21a54e8"
                },
                {
                  id: "af83cc0a-4faf-4d4b-8322-3c6c1caad65c",
                  content: "Echo back the calculated result",
                  status: "completed",
                  section_id: "c192046c-a491-4b3d-9447-bc97d21a54e8"
                }
              ]
            }
          ],
          total_tasks: 2,
          total_sections: 1,
          tool_name: "update_tasks"
        }),
      },
    },
  ];

  return (
    <div className="h-screen">
      <h1 className="p-4 text-xl font-bold">真实数据测试 - DeepResearch</h1>
      <ToolCallSidePanel
        isOpen={open}
        onClose={() => setOpen(false)}
        toolCalls={toolCalls}
        currentIndex={toolCalls.length - 1}
        onNavigate={() => {}}
        agentStatus="idle"
      />
    </div>
  );
} 