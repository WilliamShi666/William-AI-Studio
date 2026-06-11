import { useEffect, useRef, useState } from "react";
import {
  TerminalSquare,
  X,
  Maximize2,
  Minimize2,
  Plus,
  ChevronDown,
} from "lucide-react";
import { useUIStore } from "../../stores/uiStore";

interface TerminalTab {
  id: string;
  label: string;
  ws: WebSocket | null;
  lines: string[];
}

export default function TerminalPanel() {
  const { terminalOpen, terminalHeight, toggleTerminal, setTerminalHeight } =
    useUIStore();
  const projectPath = useUIStore((s) => s.projectPath);
  const [tabs, setTabs] = useState<TerminalTab[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [maximized, setMaximized] = useState(false);
  const outputRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const resizeRef = useRef<HTMLDivElement>(null);

  // Create initial terminal tab on first open
  useEffect(() => {
    if (terminalOpen && tabs.length === 0) {
      createTab();
    }
  }, [terminalOpen]);

  // Auto-scroll output
  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [tabs, activeTab]);

  const createTab = () => {
    const id = `term_${Date.now()}`;
    const host = window.location.hostname;
    const port = 3001;
    const params = new URLSearchParams({
      id,
      cwd: projectPath || "",
    });

    const ws = new WebSocket(`ws://${host}:${port}/ws/terminal?${params}`);
    const newTab: TerminalTab = { id, label: `终端 ${tabs.length + 1}`, ws, lines: [] };

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.event === "output") {
          setTabs((prev) =>
            prev.map((t) =>
              t.id === id ? { ...t, lines: [...t.lines, msg.data] } : t
            )
          );
        } else if (msg.event === "exit") {
          setTabs((prev) =>
            prev.map((t) =>
              t.id === id
                ? { ...t, lines: [...t.lines, `\n[进程已退出，代码: ${msg.code}]\n`] }
                : t
            )
          );
        }
      } catch {
        // Not JSON, raw output
      }
    };

    ws.onerror = () => {
      setTabs((prev) =>
        prev.map((t) =>
          t.id === id
            ? { ...t, lines: [...t.lines, "[连接错误]\n"] }
            : t
        )
      );
    };

    setTabs((prev) => [...prev, newTab]);
    setActiveTab(id);
  };

  const closeTab = (id: string) => {
    const tab = tabs.find((t) => t.id === id);
    if (tab?.ws) {
      tab.ws.send(JSON.stringify({ action: "close" }));
      tab.ws.close();
    }
    const remaining = tabs.filter((t) => t.id !== id);
    setTabs(remaining);
    if (remaining.length === 0) {
      setActiveTab(null);
      toggleTerminal();
    } else if (activeTab === id) {
      setActiveTab(remaining[remaining.length - 1]?.id || null);
    }
  };

  const sendInput = (input: string) => {
    const tab = tabs.find((t) => t.id === activeTab);
    if (tab?.ws?.readyState === WebSocket.OPEN) {
      tab.ws.send(JSON.stringify({ action: "input", data: input + "\n" }));
      // Echo input locally
      setTabs((prev) =>
        prev.map((t) =>
          t.id === activeTab ? { ...t, lines: [...t.lines, `$ ${input}\n`] } : t
        )
      );
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      sendInput(e.currentTarget.value);
      e.currentTarget.value = "";
    }
  };

  // Drag resize
  const handleResizeMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = terminalHeight;

    const onMove = (ev: MouseEvent) => {
      const delta = startY - ev.clientY;
      setTerminalHeight(Math.max(120, Math.min(600, startH + delta)));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  };

  if (!terminalOpen) return null;

  const currentTab = tabs.find((t) => t.id === activeTab);
  const height = maximized ? "100%" : `${terminalHeight}px`;

  return (
    <div
      className={`flex-shrink-0 flex flex-col bg-[color:var(--app-bg)] border-t border-[color:var(--app-border)] ${
        maximized ? "absolute inset-0 z-40" : ""
      }`}
      style={maximized ? {} : { height }}
    >
      {/* Resize handle */}
      {!maximized && (
        <div
          ref={resizeRef}
          onMouseDown={handleResizeMouseDown}
          className="h-1 cursor-ns-resize hover:bg-amber-glow/20 transition-colors flex-shrink-0"
        />
      )}

      {/* Tab bar */}
      <div className="flex items-center bg-[color:var(--app-surface)]/60 border-b border-[color:var(--app-border)] flex-shrink-0">
        <div className="flex items-center gap-0.5 px-1 flex-1 overflow-x-auto">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`group flex items-center gap-1.5 px-2.5 py-1.5 text-xs transition-colors rounded-t whitespace-nowrap ${
                tab.id === activeTab
                  ? "text-amber-glow bg-[color:var(--app-bg)]"
                  : "text-[color:var(--app-text-subtle)] hover:text-[color:var(--app-text-muted)] hover:bg-[color:var(--app-surface-elevated)]/40"
              }`}
            >
              <TerminalSquare size={11} />
              {tab.label}
              <X
                size={10}
                className="opacity-0 group-hover:opacity-100 hover:text-rose-err"
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(tab.id);
                }}
              />
            </button>
          ))}
        </div>

        <div className="flex items-center gap-0.5 px-2">
          {tabs.length < 5 && (
            <button
              onClick={createTab}
              className="p-1 rounded hover:bg-[color:var(--app-surface-elevated)]/50 text-[color:var(--app-text-subtle)] hover:text-[color:var(--app-text-muted)] transition-colors"
              title="新建终端"
            >
              <Plus size={12} />
            </button>
          )}
          <button
            onClick={() => setMaximized(!maximized)}
            className="p-1 rounded hover:bg-[color:var(--app-surface-elevated)]/50 text-[color:var(--app-text-subtle)] hover:text-[color:var(--app-text-muted)] transition-colors"
            title={maximized ? "还原" : "最大化"}
          >
            {maximized ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
          </button>
          <button
            onClick={toggleTerminal}
            className="p-1 rounded hover:bg-[color:var(--app-surface-elevated)]/50 text-[color:var(--app-text-subtle)] hover:text-[color:var(--app-text-muted)] transition-colors"
            title="关闭终端"
          >
            <ChevronDown size={12} />
          </button>
        </div>
      </div>

      {/* Terminal output */}
      <div
        ref={outputRef}
        className="flex-1 overflow-y-auto px-3 py-2 font-mono text-xs text-[color:var(--app-text-muted)] leading-relaxed"
      >
        {currentTab?.lines.map((line, i) => (
          <span key={i} className="whitespace-pre-wrap">
            {line}
          </span>
        ))}
      </div>

      {/* Input */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-t border-[color:var(--app-border)] bg-[color:var(--app-surface)]/30 flex-shrink-0">
        <span className="text-amber-glow text-xs font-mono">$</span>
        <input
          ref={inputRef}
          onKeyDown={handleKeyDown}
          placeholder="输入命令..."
          className="flex-1 text-xs font-mono bg-transparent text-[color:var(--app-text-muted)] placeholder-obsidian-400 focus:outline-none"
        />
      </div>
    </div>
  );
}
