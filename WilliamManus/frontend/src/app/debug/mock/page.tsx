"use client";
import React from "react";
import dynamic from "next/dynamic";

// 动态导入以避免 SSR 问题
const ToolCallSidePanel = dynamic(
  () => import("@/components/thread/tool-call-side-panel").then(mod => mod.ToolCallSidePanel),
  { ssr: false }
);

type ToolCallInput = any;

export default function MockToolStreamPage() {
  const [open, setOpen] = React.useState(true);

  // 流程：create-tasks -> view-tasks -> update-tasks(step1) -> update-tasks(step2) -> test_calculator -> update-tasks(final)
  const createdAt = "2025-09-12T00:00:04Z";
  const viewedAt = "2025-09-12T00:00:05Z";
  const step1At = "2025-09-12T00:00:06Z";
  const step2At = "2025-09-12T00:00:07Z";
  const calcAt = "2025-09-12T00:00:08Z";
  const finalAt = "2025-09-12T00:00:09Z";

  const sectionsInitial = [
    {
      id: "section-1",
      title: "Research & Setup",
      tasks: [
        { id: "task-a", content: "Understand the required mathematical calculation", status: "pending", section_id: "section-1" },
        { id: "task-b", content: "Determine the tools needed for calculation", status: "pending", section_id: "section-1" },
      ],
    },
    {
      id: "section-2",
      title: "Execution",
      tasks: [
        { id: "task-c", content: "Perform the calculation 100 * 23 * 323 * 123", status: "pending", section_id: "section-2" },
        { id: "task-d", content: "Echo back the calculated result", status: "pending", section_id: "section-2" },
      ],
    },
  ];

  // Step 1: 完成 task-a
  const sectionsStep1 = [
    {
      id: "section-1",
      title: "Research & Setup",
      tasks: [
        { id: "task-a", content: "Understand the required mathematical calculation", status: "completed", section_id: "section-1" },
        { id: "task-b", content: "Determine the tools needed for calculation", status: "pending", section_id: "section-1" },
      ],
    },
    {
      id: "section-2",
      title: "Execution",
      tasks: [
        { id: "task-c", content: "Perform the calculation 100 * 23 * 323 * 123", status: "pending", section_id: "section-2" },
        { id: "task-d", content: "Echo back the calculated result", status: "pending", section_id: "section-2" },
      ],
    },
  ];

  // Step 2: 再完成 task-b、task-c
  const sectionsStep2 = [
    {
      id: "section-1",
      title: "Research & Setup",
      tasks: [
        { id: "task-a", content: "Understand the required mathematical calculation", status: "completed", section_id: "section-1" },
        { id: "task-b", content: "Determine the tools needed for calculation", status: "completed", section_id: "section-1" },
      ],
    },
    {
      id: "section-2",
      title: "Execution",
      tasks: [
        { id: "task-c", content: "Perform the calculation 100 * 23 * 323 * 123", status: "completed", section_id: "section-2" },
        { id: "task-d", content: "Echo back the calculated result", status: "pending", section_id: "section-2" },
      ],
    },
  ];

  // Final: 全部完成
  const sectionsFinal = [
    {
      id: "section-1",
      title: "Research & Setup",
      tasks: [
        { id: "task-a", content: "Understand the required mathematical calculation", status: "completed", section_id: "section-1" },
        { id: "task-b", content: "Determine the tools needed for calculation", status: "completed", section_id: "section-1" },
      ],
    },
    {
      id: "section-2",
      title: "Execution",
      tasks: [
        { id: "task-c", content: "Perform the calculation 100 * 23 * 323 * 123", status: "completed", section_id: "section-2" },
        { id: "task-d", content: "Echo back the calculated result", status: "completed", section_id: "section-2" },
      ],
    },
  ];

  const toolCalls: ToolCallInput[] = [
    // 1) 创建任务
    {
      assistantCall: {
        name: "create-tasks",
        timestamp: createdAt,
        content: JSON.stringify({ role: "assistant", content: "Create initial task list." }),
      },
      toolResult: {
        timestamp: createdAt,
        isSuccess: true,
        content: JSON.stringify({
          sections: sectionsInitial,
          total_tasks: 4,
          total_sections: 2,
          tool_name: "create_tasks",
        }),
      },
    },
    // 2) 查看任务（应与创建时一致）
    {
      assistantCall: {
        name: "view-tasks",
        timestamp: viewedAt,
        content: JSON.stringify({ role: "assistant", content: "View current tasks." }),
      },
      toolResult: {
        timestamp: viewedAt,
        isSuccess: true,
        content: JSON.stringify({
          sections: sectionsInitial,
          total_tasks: 4,
          total_sections: 2,
          tool_name: "view_tasks",
        }),
      },
    },
    // 3) 局部更新 step1（完成 task-a）
    {
      assistantCall: {
        name: "update-tasks",
        timestamp: step1At,
        content: JSON.stringify({ role: "assistant", content: "Mark task-a as completed." }),
      },
      toolResult: {
        timestamp: step1At,
        isSuccess: true,
        content: JSON.stringify({
          sections: sectionsStep1,
          total_tasks: 4,
          total_sections: 2,
          tool_name: "update_tasks",
        }),
      },
    },
    // 4) 局部更新 step2（完成 task-b 与 task-c）
    {
      assistantCall: {
        name: "update-tasks",
        timestamp: step2At,
        content: JSON.stringify({ role: "assistant", content: "Mark task-b and task-c as completed." }),
      },
      toolResult: {
        timestamp: step2At,
        isSuccess: true,
        content: JSON.stringify({
          sections: sectionsStep2,
          total_tasks: 4,
          total_sections: 2,
          tool_name: "update_tasks",
        }),
      },
    },
    // 5) 中间工具：计算结果（GenericToolView）
    {
      assistantCall: {
        name: "test_calculator",
        timestamp: calcAt,
        content: JSON.stringify({ tool_name: "test_calculator", parameters: { operation: "multiply", a: 742900, b: 123 } }),
      },
      toolResult: {
        timestamp: calcAt,
        isSuccess: true,
        content: JSON.stringify({ tool_name: "test_calculator", result: "742900 multiply 123 = 91376700" }),
      },
    },
    // 6) 最终更新（全部完成）
    {
      assistantCall: {
        name: "update-tasks",
        timestamp: finalAt,
        content: JSON.stringify({ role: "assistant", content: "Mark all remaining tasks as completed." }),
      },
      toolResult: {
        timestamp: finalAt,
        isSuccess: true,
        content: JSON.stringify({
          sections: sectionsFinal,
          total_tasks: 4,
          total_sections: 2,
          tool_name: "update_tasks",
        }),
      },
    },
  ];

  return (
    <div className="h-screen">
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
