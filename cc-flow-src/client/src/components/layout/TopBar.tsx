import {
  PanelLeftClose,
  PanelLeftOpen,
  Folder,
  DollarSign,
  Wifi,
  WifiOff,
  TerminalSquare,
} from "lucide-react";
import { useUIStore } from "../../stores/uiStore";
import { useConfigStore } from "../../stores/configStore";
import { useChatStore } from "../../stores/chatStore";
import { formatCost } from "../../utils/costCalculator";
import ModelSelector from "../manage/ModelSelector";
import BrandLogo from "../brand/BrandLogo";
import { APP_NAME } from "../../constants/branding";

export default function TopBar() {
  const { sidebarOpen, toggleSidebar, wsConnected, projectPath, setProjectPath, toggleTerminal } =
    useUIStore();
  const model = useConfigStore((s) => s.model);
  const totalCost = useChatStore((s) => s.totalCost);

  return (
    <header className="h-12 flex-shrink-0 flex items-center border-b border-[color:var(--app-border)] bg-[color:var(--app-surface)] backdrop-blur-sm px-3 gap-3 z-20">
      {/* Sidebar toggle */}
      <button
        onClick={toggleSidebar}
        className="p-1.5 rounded-md hover:bg-[color:var(--app-hover)] text-[color:var(--app-text-muted)] hover:text-[color:var(--app-text)] transition-colors"
      >
        {sidebarOpen ? (
          <PanelLeftClose size={18} />
        ) : (
          <PanelLeftOpen size={18} />
        )}
      </button>

      {/* Logo */}
      <div className="flex items-center gap-2">
        <BrandLogo size="sm" className="!w-6 !h-6 !rounded-md" />
        <span className="font-display font-semibold text-[15px] tracking-tight text-[color:var(--app-text)]">
          {APP_NAME}
        </span>
      </div>

      {/* Divider */}
      <div className="w-px h-5 bg-[color:var(--app-border)]" />

      {/* Project path */}
      <button
        onClick={() => {
          const p = window.prompt("项目路径：", projectPath);
          if (p) setProjectPath(p);
        }}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-mono text-[color:var(--app-text-muted)] hover:text-[color:var(--app-text)] hover:bg-[color:var(--app-hover)] transition-colors max-w-[280px] truncate"
      >
        <Folder size={13} className="flex-shrink-0 text-[color:var(--app-text-subtle)]" />
        {projectPath || "选择项目..."}
      </button>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Model selector */}
      <ModelSelector />

      {/* Terminal toggle */}
      <button
        onClick={toggleTerminal}
        className="p-1.5 rounded-md hover:bg-[color:var(--app-hover)] text-[color:var(--app-text-muted)] hover:text-[color:var(--app-text)] transition-colors"
        title="终端"
      >
        <TerminalSquare size={16} />
      </button>

      {/* Cost */}
      <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[color:var(--app-control)] text-xs font-mono">
        <DollarSign size={12} className="text-amber-glow" />
        <span className="text-[color:var(--app-text-muted)]">{formatCost(totalCost)}</span>
      </div>

      {/* Connection status */}
      <div
        className={`flex items-center gap-1.5 px-2 py-1 rounded-md text-xs ${
          wsConnected
            ? "text-emerald-ok bg-emerald-ok/5"
            : "text-rose-err bg-rose-err/5"
        }`}
      >
        {wsConnected ? <Wifi size={12} /> : <WifiOff size={12} />}
        <span className="hidden sm:inline">
          {wsConnected ? "已连接" : "离线"}
        </span>
      </div>
    </header>
  );
}
