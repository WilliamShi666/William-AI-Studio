import { useEffect, useRef, useState } from 'react';
import { Home, Database, MessageSquare, Search, Settings, Zap, ChevronDown, Users, Shield, ChevronLeft, ChevronRight, Lightbulb, Sun, Moon } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { MODE_CONFIG, ModeId, ViewId, canAccessAdminPanel, getAccessibleModes } from '../src/modes';
import type { UserRole } from '../src/api/auth';

interface SidebarProps {
  activeView: ViewId;
  onNavigate: (view: ViewId) => void;
  mode: ModeId;
  onSelectMode: (mode: ModeId) => void;
  userRole: UserRole;
  isCollapsed: boolean;
  onToggleCollapse: () => void;
  visualTheme: 'dark' | 'warm';
  onToggleTheme: () => void;
}

const menuItems: { id: ViewId; label: string; icon: typeof Home; disabled: boolean; badge?: string }[] = [
  { id: 'dashboard', label: '欢迎大厅', icon: Home, disabled: false },
  { id: 'knowledge', label: '知识库管理', icon: Database, disabled: false },
  { id: 'chat', label: '对话答疑', icon: MessageSquare, disabled: false },
  { id: 'qa', label: '破题结界', icon: Lightbulb, disabled: false },
  { id: 'debate', label: '辩论竞技场', icon: Users, disabled: false },
  { id: 'retrieval', label: '检索测试', icon: Search, disabled: true, badge: '待开发' },
  { id: 'settings', label: '设置', icon: Settings, disabled: false },
  { id: 'admin', label: '管理面板', icon: Shield, disabled: false },
];

export function Sidebar({
  activeView,
  onNavigate,
  mode,
  onSelectMode,
  userRole,
  isCollapsed,
  onToggleCollapse,
  visualTheme,
  onToggleTheme,
}: SidebarProps) {
  const modeConfig = MODE_CONFIG[mode];
  const [showModeMenu, setShowModeMenu] = useState(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);
  const [portalOrigin] = useState(() => {
    if (typeof window === 'undefined') {
      return '';
    }
    return `${window.location.protocol}//${window.location.hostname}`;
  });
  const homeHref = portalOrigin ? `${portalOrigin}/` : '/';

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setShowModeMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const visibleMenuItems = menuItems.filter((item) => {
    if (item.id === 'admin') {
      return canAccessAdminPanel(userRole);
    }
    return modeConfig.allowedViews.includes(item.id);
  });
  const accessibleModes = getAccessibleModes(userRole);
  const sidebarWidth = isCollapsed ? 88 : 260;

  const sidebarBg = visualTheme === 'warm' && mode !== 'v2'
    ? 'bg-[#F0EBE3]'
    : mode === 'roys'
      ? 'bg-[#152347]/60 backdrop-blur-[20px]'
      : 'bg-[#1A2540]';

  const borderColor = visualTheme === 'warm' && mode !== 'v2'
    ? 'border-[rgba(180,160,130,0.2)]'
    : 'border-[rgba(255,255,255,0.03)]';

  return (
    <div
      className={`h-screen ${sidebarBg} fixed left-0 top-0 flex flex-col z-50 border-r ${borderColor} transition-[width] duration-300 ease-in-out`}
      style={{ width: `${sidebarWidth}px` }}
    >
      {/* Logo/Brand */}
      <div className={`h-16 flex items-center border-b border-[rgba(255,255,255,0.03)] ${isCollapsed ? 'justify-between px-3' : 'justify-between px-6'}`}>
        <motion.div
          className="flex items-center gap-2"
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.5 }}
        >
          {!isCollapsed && (
            mode === 'roys' ? (
              <img src="/tutor/2333333.png" alt="Roys Legion Logo" className="w-12 h-12 rounded-lg object-cover" />
            ) : (
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center animate-pulse-glow">
                <Zap size={18} className="text-[#0F1724]" />
              </div>
            )
          )}
          {!isCollapsed && (
            mode === 'roys' ? (
              <span className="text-gradient text-lg font-semibold">Roys Legion</span>
            ) : (
              <h1 className="text-gradient">多模态RAG系统</h1>
            )
          )}
        </motion.div>
        <div className="flex items-center gap-2">
          {!isCollapsed && mode !== 'roys' && (
            <motion.div
              className="px-2 py-1 rounded-md text-xs font-semibold bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F1724]"
              initial={{ opacity: 0, x: 10 }}
              animate={{ opacity: 1, x: 0 }}
            >
              {modeConfig.badge}
            </motion.div>
          )}
          <motion.button
            onClick={onToggleCollapse}
            className="w-12 h-12 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            aria-label={isCollapsed ? '展开侧边栏' : '收起侧边栏'}
          >
            {isCollapsed ? (
              <ChevronRight size={24} className="text-[#38BDF8]" />
            ) : (
              <ChevronLeft size={24} className="text-[#38BDF8]" />
            )}
          </motion.button>
        </div>
      </div>

      {/* Navigation Menu */}
      <nav className={`flex-1 py-6 space-y-1 ${isCollapsed ? 'px-2' : 'px-4'}`}>
        {visibleMenuItems.map((item, index) => {
          const Icon = item.icon;
          const isActive = activeView === item.id;
          const isDisabled = item.disabled;

          return (
            <motion.button
              key={item.id}
              onClick={() => !isDisabled && onNavigate(item.id)}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, delay: index * 0.1 }}
              disabled={isDisabled}
              className={`w-full flex items-center rounded-xl transition-all duration-300 group ${
                isDisabled
                  ? 'text-[#64748B] opacity-50 cursor-not-allowed'
                  : isActive
                  ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F1724] shadow-[0_0_20px_rgba(56,189,248,0.4)]'
                  : 'text-[#CBD5E1] hover:bg-[rgba(56,189,248,0.1)]'
              } ${isCollapsed ? 'justify-center px-3 py-3' : 'justify-between gap-3 px-4 py-3'}`}
              title={item.label}
            >
              <div className={`flex items-center ${isCollapsed ? '' : 'gap-3'}`}>
                <Icon size={20} className={`${isDisabled ? 'text-[#64748B]' : isActive ? 'text-[#0F1724]' : 'group-hover:text-[#38BDF8]'} transition-colors`} />
                {!isCollapsed && <span>{item.label}</span>}
              </div>
              {!isCollapsed && item.badge && (
                <span className="px-2 py-0.5 text-xs bg-[rgba(255,184,0,0.15)] text-[#ffb800] rounded-full border border-[rgba(255,184,0,0.3)]">
                  {item.badge}
                </span>
              )}
            </motion.button>
          );
        })}
        <motion.a
          href={homeHref}
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.3, delay: visibleMenuItems.length * 0.1 }}
          className={`w-full flex items-center rounded-xl transition-all duration-300 group text-[#CBD5E1] hover:bg-[rgba(56,189,248,0.1)] ${isCollapsed ? 'justify-center px-3 py-3' : 'gap-3 px-4 py-3'}`}
          title="🏠 回到导航页"
        >
          <Home size={20} className="group-hover:text-[#38BDF8] transition-colors" />
          {!isCollapsed && <span>🏠 回到导航页</span>}
        </motion.a>
      </nav>

      {/* Footer */}
      <div className={`border-t ${borderColor} relative ${isCollapsed ? 'px-3 py-4' : 'px-6 py-4'}`} ref={dropdownRef}>
        {/* Theme Toggle */}
        {mode !== 'v2' && (
          <motion.button
            onClick={onToggleTheme}
            className={`w-full flex items-center gap-2 rounded-xl glass-strong border ${borderColor} ${isCollapsed ? 'justify-center px-2 py-2.5' : 'px-4 py-2.5'} mb-2 text-xs transition-colors cursor-pointer`}
            whileHover={{ scale: 1.01 }}
            whileTap={{ scale: 0.98 }}
            title={visualTheme === 'dark' ? '切换到暖色模式' : '切换到深色模式'}
          >
            {visualTheme === 'dark' ? <Sun size={14} className="text-[#F59E0B]" /> : <Moon size={14} className="text-[#2563EB]" />}
            {!isCollapsed && (
              <span className="text-[#94A3B8]">{visualTheme === 'dark' ? '切换到暖色模式' : '切换到深色模式'}</span>
            )}
          </motion.button>
        )}

        <motion.button
          onClick={() => setShowModeMenu((prev) => !prev)}
          className={`text-[#94A3B8] text-xs flex items-center hover:text-[#38BDF8] transition-colors cursor-pointer w-full glass-strong rounded-xl border border-[rgba(255,255,255,0.05)] ${isCollapsed ? 'justify-center px-2 py-3' : 'justify-between gap-4 px-4 py-3'}`}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.8 }}
          whileHover={{ scale: 1.01 }}
          whileTap={{ scale: 0.98 }}
        >
          {isCollapsed ? (
            mode === 'roys' ? (
              <img src="/tutor/2333333.png" alt="Roys Legion Logo" className="w-12 h-12 rounded-lg object-cover" />
            ) : (
              <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center text-[#0F1724] font-semibold">
                {modeConfig.badge}
              </div>
            )
          ) : (
            <>
              <div className="flex items-start gap-2 text-left">
                <div className="w-2 h-2 rounded-full bg-[#34D399] mt-1 animate-pulse" />
                <div>
                  {mode === 'roys' ? (
                    <>
                      <span className="font-medium block text-sm text-[#CBD5E1]">Roys Legion</span>
                      <span className="text-[11px] text-[#94A3B8] block">乐亦思国际学科助教军团</span>
                    </>
                  ) : (
                    <>
                      <span className="font-medium block text-sm text-[#CBD5E1]">{modeConfig.label}</span>
                      <span className="text-[11px] text-[#64748B] block mt-0.5">
                        {modeConfig.description}
                      </span>
                    </>
                  )}
                </div>
              </div>
              <ChevronDown size={16} className={`transition-transform ${showModeMenu ? 'rotate-180' : ''}`} />
            </>
          )}
        </motion.button>

        <AnimatePresence>
          {showModeMenu && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 10 }}
              transition={{ duration: 0.2 }}
              className={`absolute bottom-20 glass-strong rounded-xl border border-[rgba(255,255,255,0.05)] shadow-xl overflow-hidden ${
                isCollapsed ? 'left-16 w-64' : 'left-6 right-6'
              }`}
            >
              {accessibleModes.map((modeId) => {
                const option = MODE_CONFIG[modeId];
                const isActive = modeId === mode;
                return (
                  <button
                    key={modeId}
                    onClick={() => {
                      onSelectMode(modeId);
                      setShowModeMenu(false);
                    }}
                    className={`w-full px-4 py-3 flex items-start gap-3 text-left transition-colors ${
                      isActive ? 'bg-[rgba(56,189,248,0.1)] text-[#38BDF8]' : 'hover:bg-[rgba(56,189,248,0.08)] text-[#94A3B8]'
                    }`}
                  >
                    <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center text-[#0F1724] font-semibold">
                      {option.badge}
                    </div>
                    <div className="flex-1">
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-sm text-[#CBD5E1]">{option.label}</span>
                        {isActive && <span className="text-[10px] text-[#34D399]">当前</span>}
                      </div>
                      <p className="text-[11px] text-[#64748B] mt-1">{option.description}</p>
                      {modeId === 'roys' && (
                        <p className="text-[11px] text-[#94A3B8] mt-1">{option.brandName}</p>
                      )}
                    </div>
                  </button>
                );
              })}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
