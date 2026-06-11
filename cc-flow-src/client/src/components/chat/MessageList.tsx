import { useState, useCallback, useEffect } from "react";
import { Loader2, Image as ImageIcon, FileText, RotateCcw } from "lucide-react";
import { useChatStore } from "../../stores/chatStore";
import { useAutoScroll } from "../../hooks/useAutoScroll";
import MarkdownRenderer from "../shared/MarkdownRenderer";
import ToolCallCard from "./ToolCallCard";
import ErrorBoundary from "../shared/ErrorBoundary";
import TaskResultCard from "./TaskResultCard";
import CompactDivider from "./CompactDivider";
import { api } from "../../services/api";
import type { Session } from "../../types/session";
import BrandLogo from "../brand/BrandLogo";
import { APP_NAME } from "../../constants/branding";

function formatTime(ts: number) {
  return new Date(ts).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function MessageList() {
  const { messages, isStreaming, streamingText, statusText, hasMoreHistory, historyOffset, currentSessionId, loadHistoryMessages } = useChatStore();
  const { containerRef, handleScroll } = useAutoScroll([messages, streamingText], isStreaming);
  const [loadingMore, setLoadingMore] = useState(false);

  const handleLoadMore = useCallback(async () => {
    if (!currentSessionId || loadingMore || !hasMoreHistory) return;
    setLoadingMore(true);
    try {
      const res = await api.getSessionMessages(currentSessionId, {
        offset: historyOffset,
        limit: 50,
      });
      if (res.messages.length > 0) {
        loadHistoryMessages(
          res.messages as Parameters<typeof loadHistoryMessages>[0],
          res.total,
          historyOffset
        );
      }
    } catch {
      /* ignore */
    } finally {
      setLoadingMore(false);
    }
  }, [currentSessionId, loadingMore, hasMoreHistory, historyOffset, loadHistoryMessages]);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 min-h-0 overflow-y-auto"
    >
      <div className="max-w-4xl mx-auto px-6 py-6 space-y-8">
        {/* Load more history button */}
        {hasMoreHistory && (
          <div className="flex justify-center pt-2">
            <button
              onClick={handleLoadMore}
              disabled={loadingMore}
              className="flex items-center gap-2 px-4 py-1.5 rounded-full border border-[color:var(--app-border)] text-xs text-[color:var(--app-text-muted)] hover:text-[color:var(--app-text)] hover:border-[color:var(--app-border-strong)] bg-white/3 hover:bg-white/6 transition-all disabled:opacity-50"
            >
              {loadingMore ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <span>↑</span>
              )}
              加载更早的消息
            </button>
          </div>
        )}

        {messages.length === 0 && !isStreaming && <WelcomeScreen />}

        {(() => {
          // Find the index of the last compact divider to dim messages before it
          const lastCompactIdx = messages.reduce(
            (acc, m, i) => (m.role === "system" && m.compactData ? i : acc), -1
          );

          // Group consecutive rolledBack messages into collapsible groups
          type DisplayItem =
            | { kind: "msg"; msg: typeof messages[0]; idx: number }
            | { kind: "rolledBackGroup"; items: { msg: typeof messages[0]; idx: number }[] };

          const displayItems: DisplayItem[] = [];
          let mi = 0;
          while (mi < messages.length) {
            if (messages[mi].rolledBack) {
              const group: { msg: typeof messages[0]; idx: number }[] = [];
              while (mi < messages.length && messages[mi].rolledBack) {
                group.push({ msg: messages[mi], idx: mi });
                mi++;
              }
              displayItems.push({ kind: "rolledBackGroup", items: group });
            } else {
              displayItems.push({ kind: "msg", msg: messages[mi], idx: mi });
              mi++;
            }
          }

          return displayItems.map((item, di) => {
            if (item.kind === "rolledBackGroup") {
              return <RolledBackGroup key={`rb-${di}`} items={item.items} />;
            }

            const { msg, idx } = item;
            const isDimmed = lastCompactIdx >= 0 && idx < lastCompactIdx;
            const isCurrentAssistant = isStreaming && msg.role === "assistant" &&
              msg.id === useChatStore.getState().currentAssistantId;

            // Skip empty assistant messages that have no visible content at all
            if (
              msg.role === "assistant" &&
              !msg.content &&
              !msg.toolCalls?.length &&
              !msg.taskResult &&
              !msg.thinking &&
              !isCurrentAssistant
            ) {
              return null;
            }

            return (
              <div key={msg.id} className={isDimmed ? "opacity-50" : ""}>
                {/* Hint banner at the very first dimmed message */}
                {isDimmed && idx === 0 && (
                  <div className="text-[10px] text-[color:var(--app-text-subtle)] text-center mb-3 px-4 py-1 rounded bg-white/3 border border-[color:var(--app-border)]">
                    以下为完整历史记录 — Claude 已将这些内容压缩为摘要，不再逐条记忆
                  </div>
                )}
                {msg.role === "system" && msg.compactData ? (
                  <CompactDivider
                    tokensBefore={msg.compactData.tokensBefore}
                    tokensAfter={msg.compactData.tokensAfter}
                    summary={msg.compactData.summary}
                  />
                ) : msg.role === "user" ? (
                  <UserBubble content={msg.content} timestamp={msg.timestamp} attachments={msg.attachments} />
                ) : (
                  <AssistantBubble
                    content={msg.content}
                    streamingText={isCurrentAssistant ? streamingText : undefined}
                    thinking={msg.thinking}
                    toolCalls={msg.toolCalls}
                    taskResult={msg.taskResult}
                    timestamp={msg.timestamp}
                    isStreaming={isCurrentAssistant}
                    statusText={isCurrentAssistant ? statusText : undefined}
                  />
                )}
              </div>
            );
          });
        })()}

      </div>
    </div>
  );
}

/* ── Rolled-back messages: collapsible group ── */
function RolledBackGroup({
  items,
}: {
  items: { msg: import("../../types/claude").ChatMessage; idx: number }[];
}) {
  const [expanded, setExpanded] = useState(false);
  const count = items.length;

  return (
    <div>
      {/* Divider + summary toggle */}
      <div className="flex items-center gap-3 py-2 my-2">
        <div className="flex-1 h-px bg-amber-glow/20" />
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1.5 px-3 py-1 rounded-full
                     bg-amber-glow/5 border border-amber-glow/15
                     text-[11px] text-amber-glow/70 hover:text-amber-glow
                     hover:bg-amber-glow/10 transition-colors"
        >
          <RotateCcw size={10} />
          <span>{count} 条已回滚消息</span>
          <span className="text-[9px] text-[color:var(--app-text-subtle)] ml-1">
            {expanded ? "收起" : "展开"}
          </span>
        </button>
        <div className="flex-1 h-px bg-amber-glow/20" />
      </div>

      {/* Expanded: show original messages with reduced opacity + amber left border */}
      {expanded && (
        <div className="opacity-40 border-l-2 border-amber-glow/15 pl-4 ml-2 space-y-8">
          {items.map(({ msg }) => (
            <div key={msg.id}>
              {msg.role === "system" && msg.compactData ? (
                <CompactDivider
                  tokensBefore={msg.compactData.tokensBefore}
                  tokensAfter={msg.compactData.tokensAfter}
                />
              ) : msg.role === "user" ? (
                <UserBubble content={msg.content} timestamp={msg.timestamp} attachments={msg.attachments} />
              ) : (
                <AssistantBubble
                  content={msg.content}
                  thinking={msg.thinking}
                  toolCalls={msg.toolCalls}
                  taskResult={msg.taskResult}
                  timestamp={msg.timestamp}
                />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── User avatar: round, emoji ── */
function UserAvatar() {
  return (
    <div className="w-10 h-10 rounded-full bg-[color:var(--app-control)] flex-shrink-0 flex items-center justify-center border border-[color:var(--app-border)] text-xl select-none">
      🧑‍💻
    </div>
  );
}

/* ── Assistant avatar ── */
function AiAvatar() {
  return (
    <BrandLogo
      size="sm"
      shape="circle"
      className="!w-10 !h-10 flex-shrink-0"
    />
  );
}

/* ── User message bubble ── */
function UserBubble({ content, timestamp, attachments }: { content: string; timestamp?: number; attachments?: import("../../types/claude").Attachment[] }) {
  return (
    <div className="flex gap-4">
      <UserAvatar />
      <div className="flex flex-col gap-2 min-w-0 max-w-[85%]">
        <div className="flex items-baseline gap-3">
          <span className="text-sm font-bold text-[color:var(--app-text)]">你</span>
          {timestamp && (
            <span className="text-xs text-[color:var(--app-text-subtle)]">{formatTime(timestamp)}</span>
          )}
        </div>
        <div className="w-fit px-4 py-2.5 rounded-2xl rounded-tl-none bg-[color:var(--app-hover)] border border-[color:var(--app-border)] text-[color:var(--app-text-muted)] leading-relaxed text-sm">
          <MarkdownRenderer content={content} />
          {attachments && attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {attachments.map((a) => (
                <span
                  key={a.id}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-[color:var(--app-hover)] border border-white/8 text-xs text-[color:var(--app-text-muted)]"
                >
                  {a.type.startsWith("image/") ? (
                    <ImageIcon size={12} className="text-[color:var(--app-accent)]/70" />
                  ) : (
                    <FileText size={12} className="text-amber-glow/70" />
                  )}
                  {a.name}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Tool call grouping (D9) ─────────────────────────────────

type ToolCallDisplayItem =
  | { type: "single"; tc: import("../../types/claude").ToolCall }
  | { type: "group"; toolName: string; items: import("../../types/claude").ToolCall[] };

/** Group ≥3 consecutive same-type tool calls into a foldable group. */
function groupToolCalls(
  toolCalls: import("../../types/claude").ToolCall[],
  threshold = 3
): ToolCallDisplayItem[] {
  const result: ToolCallDisplayItem[] = [];
  let i = 0;
  while (i < toolCalls.length) {
    const name = toolCalls[i].toolName;
    let j = i + 1;
    while (j < toolCalls.length && toolCalls[j].toolName === name) j++;
    const count = j - i;
    if (count >= threshold) {
      result.push({ type: "group", toolName: name, items: toolCalls.slice(i, j) });
    } else {
      for (let k = i; k < j; k++) result.push({ type: "single", tc: toolCalls[k] });
    }
    i = j;
  }
  return result;
}

const GROUP_LABELS: Record<string, string> = {
  Read: "读取了",
  Glob: "扫描了",
  Grep: "搜索了",
  Edit: "编辑了",
  Write: "创建了",
  Bash: "执行了",
  WebFetch: "请求了",
  WebSearch: "搜索了",
  Task: "派发了",
};
const GROUP_UNITS: Record<string, string> = {
  Read: "个文件",
  Glob: "次目录",
  Grep: "次搜索",
  Edit: "个文件",
  Write: "个文件",
  Bash: "条命令",
  WebFetch: "个链接",
  WebSearch: "次搜索",
  Task: "个子任务",
};

function ToolCallGroup({
  toolName,
  items,
}: {
  toolName: string;
  items: import("../../types/claude").ToolCall[];
}) {
  const [expanded, setExpanded] = useState(false);
  const label = GROUP_LABELS[toolName] || "调用了";
  const unit  = GROUP_UNITS[toolName]  || "次";
  const allDone  = items.every((tc) => tc.status === "done");
  const anyError = items.some((tc) => tc.status === "error");
  const running  = items.some((tc) => tc.status === "running");

  return (
    <div className="my-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-white/8 bg-white/3 hover:bg-white/6 transition-colors w-full text-left"
      >
        <span className={`text-[11px] ${anyError ? "text-rose-err" : allDone ? "text-emerald-ok/70" : "text-amber-glow/70"}`}>
          {anyError ? "✗" : allDone ? "✓" : running ? "…" : "○"}
        </span>
        <span className="text-[11px] text-[color:var(--app-text-muted)] flex-1">
          {label} <span className="text-[color:var(--app-text-muted)] font-medium">{items.length}</span> {unit}
          <span className="text-[color:var(--app-text-subtle)] ml-1">({toolName})</span>
        </span>
        <span className="text-[10px] text-[color:var(--app-text-subtle)]">{expanded ? "收起 ▲" : "展开 ▼"}</span>
      </button>
      {expanded && (
        <div className="pl-3 mt-1">
          {items.map((tc) => <ToolCallCard key={tc.id} toolCall={tc} />)}
        </div>
      )}
    </div>
  );
}

function renderToolCalls(toolCalls: import("../../types/claude").ToolCall[]) {
  return groupToolCalls(toolCalls).map((item, idx) =>
    item.type === "single"
      ? <ErrorBoundary key={item.tc.id} scope="ToolCall"><ToolCallCard toolCall={item.tc} /></ErrorBoundary>
      : <ToolCallGroup key={`grp-${idx}-${item.toolName}`} toolName={item.toolName} items={item.items} />
  );
}

/* ── Thinking block (collapsible) ── */
function ThinkingBlock({ thinking }: { thinking: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="my-2 border-l-2 border-[color:color-mix(in_srgb,var(--app-accent)_30%,transparent)] rounded-r-lg overflow-hidden bg-[color:color-mix(in_srgb,var(--app-accent)_6%,transparent)]">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-[color:color-mix(in_srgb,var(--app-accent)_8%,transparent)] transition-colors"
      >
        <span className="text-[color:var(--app-accent)]/70 text-[11px]">💭</span>
        <span className="text-[11px] text-[color:var(--app-accent)]/70 font-medium flex-1">思考过程</span>
        <span className="text-[10px] text-[color:var(--app-text-subtle)]">{open ? "收起 ▲" : "展开 ▼"}</span>
      </button>
      {open && (
        <div className="px-4 py-2 border-t border-[color:color-mix(in_srgb,var(--app-accent)_16%,transparent)]">
          <pre className="text-xs font-mono text-[color:var(--app-text-muted)]/80 whitespace-pre-wrap leading-relaxed max-h-72 overflow-y-auto">
            {thinking}
          </pre>
        </div>
      )}
    </div>
  );
}

/* ── Assistant message bubble ── */
function AssistantBubble({
  content,
  streamingText: liveText,
  thinking,
  toolCalls,
  taskResult,
  isStreaming,
  timestamp,
  statusText,
}: {
  content: string;
  streamingText?: string;
  thinking?: string;
  toolCalls?: import("../../types/claude").ToolCall[];
  taskResult?: import("../../types/claude").TaskResult;
  isStreaming?: boolean;
  timestamp?: number;
  statusText?: string;
}) {
  return (
    <div className="flex gap-4">
      <AiAvatar />
      <div className="flex flex-col gap-2 flex-1 min-w-0">
        <div className="flex items-baseline gap-3">
          <span className="text-sm font-bold text-[color:var(--app-text)]">Claude Code</span>
          {timestamp && !isStreaming && (
            <span className="text-xs text-[color:var(--app-text-subtle)]">{formatTime(timestamp)}</span>
          )}
        </div>
        <div className="min-w-0">
          {thinking && <ThinkingBlock thinking={thinking} />}
          {/* Committed text */}
          {content && (
            <div className="text-[color:var(--app-text-muted)] leading-relaxed text-sm">
              <MarkdownRenderer content={content} />
            </div>
          )}
          {/* Live streaming text — also rendered as markdown for consistent formatting */}
          {liveText && (
            <div className="text-[color:var(--app-text-muted)] leading-relaxed text-sm streaming-cursor">
              <MarkdownRenderer content={liveText} />
            </div>
          )}
          {/* Status indicator: shows when streaming but no text content yet */}
          {isStreaming && !content && !liveText && statusText && (
            <div className="flex items-center gap-2 py-1">
              <span className="agent-pulse-ring w-2 h-2 rounded-full bg-emerald-ok flex-shrink-0" />
              <span className="text-sm text-[color:var(--app-text-muted)] animate-pulse">
                {statusText}
              </span>
            </div>
          )}
          {toolCalls && renderToolCalls(toolCalls)}
          {taskResult && <TaskResultCard result={taskResult} />}
        </div>
      </div>
    </div>
  );
}

/* ── Welcome screen ── */
function WelcomeScreen() {
  const [recentSession, setRecentSession] = useState<Session | null>(null);
  const { setSessionId, loadHistoryMessages } = useChatStore();

  useEffect(() => {
    api.getSessions(undefined)
      .then((res) => {
        const sessions = res.sessions as Session[];
        if (sessions.length > 0) setRecentSession(sessions[0]);
      })
      .catch(() => { /* ignore */ });
  }, []);

  const handleResume = async () => {
    if (!recentSession) return;
    setSessionId(recentSession.id);
    try {
      const res = await api.getSessionMessages(recentSession.id, { offset: 0, limit: 50 });
      if (res.messages.length > 0) {
        loadHistoryMessages(
          res.messages as Parameters<typeof loadHistoryMessages>[0],
          res.total,
          0
        );
      }
    } catch { /* ignore */ }
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-[55vh] text-center px-4 select-none">
      {/* Logo */}
      <div className="relative mb-8">
        <div
          className="absolute inset-0 rounded-3xl blur-2xl scale-125"
          style={{ background: "var(--app-glow)" }}
        />
        <BrandLogo size="lg" shape="circle" className="relative" />
      </div>

      <h1 className="font-display text-2xl font-bold text-[color:var(--app-text)] mb-2 tracking-tight">
        {APP_NAME}
      </h1>
      <p className="text-sm text-[color:var(--app-text-muted)] max-w-md leading-relaxed mb-6">
        Claude Code 清晰呈现在一个安全的工作台里。<br />
        描述你想构建的内容，让 Claude Code 协助你完成项目。
      </p>

      {/* Resume recent session */}
      {recentSession && (
        <button
          onClick={handleResume}
          className="flex items-center gap-2 px-5 py-2.5 mb-6 rounded-xl bg-[color:color-mix(in_srgb,var(--app-accent)_12%,transparent)] border border-[color:color-mix(in_srgb,var(--app-accent)_24%,transparent)] hover:bg-[color:color-mix(in_srgb,var(--app-accent)_16%,transparent)] hover:border-[color:var(--app-accent)] transition-all text-sm"
        >
          <RotateCcw size={14} className="text-[color:var(--app-accent)]" />
          <span className="text-[color:var(--app-text-muted)]">继续最近会话：</span>
          <span className="text-[color:var(--app-accent-hover)] font-medium truncate max-w-[200px]">
            {recentSession.name || recentSession.summary || recentSession.id.slice(0, 12) + "…"}
          </span>
        </button>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 max-w-lg w-full">
        {[
          { title: "搭建新项目", desc: "用 TypeScript 创建一个 Express API 服务" },
          { title: "修复 Bug",   desc: "找到并修复登录认证流程的问题" },
          { title: "编写测试",   desc: "为用户模块添加完整的单元测试" },
          { title: "重构代码",   desc: "对数据库访问层进行现代化改造" },
        ].map((hint) => (
          <button
            key={hint.title}
            onClick={() => useChatStore.getState().addUserMessage(hint.desc)}
            className="text-left p-3.5 rounded-xl border border-[color:var(--app-border)] hover:border-[color:var(--app-accent)] hover:bg-[color:var(--app-hover)] transition-all group"
            style={{ background: "var(--app-surface)" }}
          >
            <div className="text-sm font-semibold text-[color:var(--app-text)] group-hover:text-[color:var(--app-text)] transition-colors mb-0.5">
              {hint.title}
            </div>
            <div className="text-xs text-[color:var(--app-text-muted)] leading-relaxed">
              {hint.desc}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
