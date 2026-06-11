import { useEffect } from "react";
import Sidebar from "./Sidebar";
import RightPanel from "./RightPanel";
import ChatPanel from "../chat/ChatPanel";
import HistoryModal from "../modals/HistoryModal";
import FileViewModal from "../modals/FileViewModal";
import SettingsModal from "../modals/SettingsModal";
import FolderBrowserModal from "../modals/FolderBrowserModal";
import SkillBrowserModal from "../modals/SkillBrowserModal";
import CreateSkillModal from "../modals/CreateSkillModal";
import { useSystemStore } from "../../stores/systemStore";
import { useDoubleEsc } from "../../hooks/useDoubleEsc";

export default function AppLayout() {
  const { loadClaudeInfo, loadAuthStatus, loadClaudeSettings } = useSystemStore();

  useEffect(() => {
    loadClaudeInfo();
    loadAuthStatus();
    loadClaudeSettings();
  }, [loadClaudeInfo, loadAuthStatus, loadClaudeSettings]);

  useDoubleEsc();

  return (
    <div className="h-screen flex overflow-hidden bg-[color:var(--app-bg)]">

      <Sidebar />

      <div className="relative flex-1 flex overflow-hidden min-w-0">
        <div className="absolute inset-0 pointer-events-none overflow-hidden" style={{ zIndex: 0 }}>
          <div
            className="absolute rounded-full blur-[160px]"
            style={{
              top: "-18%", left: "10%", width: "48%", height: "38%",
              background: "rgba(184, 100, 69, 0.08)",
            }}
          />
          <div
            className="absolute rounded-full blur-[180px]"
            style={{
              top: "-8%", right: "-10%", width: "26%", height: "22%",
              background: "rgba(217, 119, 87, 0.08)",
            }}
          />
        </div>

        <div className="relative z-10 flex flex-1 overflow-hidden">
          <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
            <ChatPanel />
          </div>
          <RightPanel />
        </div>
      </div>

      {/* Global modals */}
      <HistoryModal />
      <FileViewModal />
      <SettingsModal />
      <FolderBrowserModal />
      <SkillBrowserModal />
      <CreateSkillModal />
    </div>
  );
}
