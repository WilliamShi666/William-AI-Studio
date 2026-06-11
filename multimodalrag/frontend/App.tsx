import { useEffect, useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { Sidebar } from './components/Sidebar';
import { Header } from './components/Header';
import { Dashboard } from './components/Dashboard';
import { KnowledgeBase } from './components/KnowledgeBase';
import { KnowledgeBaseDetail } from './components/KnowledgeBaseDetail';
import { DocumentViewer } from './components/DocumentViewer';
import { Chat } from './components/Chat';
import { QuestionSolver } from './components/QuestionSolver';
import { DebateArena } from './components/DebateArena';
import { RetrievalTest } from './components/RetrievalTest';
import { Settings } from './components/Settings';
import { AdminPanel } from './components/AdminPanel';
import { Toaster } from './components/ui/sonner';
import { toast } from 'sonner';
import { MODE_CONFIG, ModeId, ViewId, getFirstAllowedView, getAccessibleModes, canAccessAdminPanel, isDarkThemeMode } from './src/modes';
import { authFetch, getAuthToken, getUserRole, parseJwtPayload, isTokenExpired, redirectToLogin, UserRole } from './src/api/auth';
import { config } from './src/config';
import { ParticleBackground } from './components/ParticleBackground';

export default function App() {
  const [activeView, setActiveView] = useState<ViewId>('dashboard');
  const [selectedKnowledgeBase, setSelectedKnowledgeBase] = useState<string | null>(null);
  const [selectedDocument, setSelectedDocument] = useState<string | null>(null);
  const [userRole, setUserRole] = useState<UserRole>(() => getUserRole());
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(() => {
    if (typeof window === 'undefined') return false;
    return localStorage.getItem('sidebar_collapsed') === 'true';
  });
  const [isSecondarySidebarOpen, setIsSecondarySidebarOpen] = useState(() => {
    if (typeof window === 'undefined') return true;
    return localStorage.getItem('secondary_sidebar_open') !== 'false';
  });
  
  // 从localStorage读取模式状态，默认v1
  const [mode, setMode] = useState<ModeId>(() => {
    const saved = localStorage.getItem('rag_mode') as ModeId | null;
    const role = getUserRole();
    const accessible = getAccessibleModes(role);
    if (saved && accessible.includes(saved)) {
      return saved;
    }
    return accessible[0] ?? 'roys';
  });

  // 视觉主题切换（深色/暖色）
  const [visualTheme, setVisualTheme] = useState<'dark' | 'warm'>(() => {
    if (typeof window === 'undefined') return 'dark';
    return (localStorage.getItem('visual_theme') as 'dark' | 'warm') || 'dark';
  });

  useEffect(() => {
    localStorage.setItem('visual_theme', visualTheme);
  }, [visualTheme]);

  const toggleVisualTheme = () => {
    setVisualTheme(prev => prev === 'dark' ? 'warm' : 'dark');
  };

  // 在应用启动时检查Token有效性，如果过期则跳转到登录页
  useEffect(() => {
    if (typeof window === 'undefined') return;

    const token = getAuthToken();

    // 没有Token - 跳转到登录页
    if (!token) {
      console.warn('No auth token found, redirecting to login');
      redirectToLogin();
      return;
    }

    // Token已过期 - 跳转到登录页
    if (isTokenExpired(token)) {
      console.warn('Auth token expired, redirecting to login');
      redirectToLogin();
      return;
    }
  }, []); // 只在组件挂载时运行一次

  useEffect(() => {
    let isMounted = true;

    const syncRole = async () => {
      const token = getAuthToken();
      if (!token) {
        if (isMounted) setUserRole('user');
        return;
      }

      // 在发起API请求前检查Token是否过期
      if (isTokenExpired(token)) {
        if (isMounted) {
          redirectToLogin();
        }
        return;
      }

      const payload = parseJwtPayload(token);
      if (payload?.role) {
        if (isMounted) setUserRole(payload.role);
        return;
      }

      try {
        // 使用 skipAuthRedirect 选项，以便我们可以自己处理 401
        const response = await authFetch(
          `${config.mainApiUrl}/auth/me`,
          {},
          { skipAuthRedirect: true }
        );

        if (response.status === 401) {
          // Session 在服务器端无效，跳转到登录页
          if (isMounted) {
            redirectToLogin();
          }
          return;
        }

        if (!response.ok) return;

        const data = await response.json();
        const apiRole = data?.user?.role;
        if (apiRole === 'admin' && isMounted) {
          setUserRole('admin');
        }
      } catch {
        // 网络错误 - 不跳转，让用户重试
        console.warn('Failed to sync role, network error');
      }
    };

    syncRole();

    const handleStorage = () => {
      syncRole();
    };
    window.addEventListener('storage', handleStorage);

    return () => {
      isMounted = false;
      window.removeEventListener('storage', handleStorage);
    };
  }, []);

  useEffect(() => {
    localStorage.setItem('rag_mode', mode);
  }, [mode]);

  useEffect(() => {
    localStorage.setItem('sidebar_collapsed', String(isSidebarCollapsed));
  }, [isSidebarCollapsed]);

  useEffect(() => {
    localStorage.setItem('secondary_sidebar_open', String(isSecondarySidebarOpen));
  }, [isSecondarySidebarOpen]);

  useEffect(() => {
    if (activeView !== 'debate') return;

    const applyAutoCollapse = () => {
      if (window.innerWidth < 1200) {
        setIsSecondarySidebarOpen(false);
      }
    };

    applyAutoCollapse();
    window.addEventListener('resize', applyAutoCollapse);
    return () => window.removeEventListener('resize', applyAutoCollapse);
  }, [activeView]);

  useEffect(() => {
    if (activeView === 'admin') {
      if (!canAccessAdminPanel(userRole)) {
        const fallback = getFirstAllowedView(mode);
        setActiveView(fallback);
      }
      return;
    }

    if (!MODE_CONFIG[mode].allowedViews.includes(activeView)) {
      const fallback = getFirstAllowedView(mode);
      setActiveView(fallback);
      setSelectedKnowledgeBase(null);
      setSelectedDocument(null);
    }
  }, [mode, activeView, userRole]);

  useEffect(() => {
    const accessible = getAccessibleModes(userRole);
    if (!accessible.includes(mode)) {
      const nextMode = accessible[0] ?? 'roys';
      setMode(nextMode);
      localStorage.setItem('rag_mode', nextMode);
      setActiveView(getFirstAllowedView(nextMode));
      setSelectedKnowledgeBase(null);
      setSelectedDocument(null);
    }
  }, [userRole, mode]);

  const handleSelectMode = (nextMode: ModeId) => {
    if (nextMode === mode) return;
    const accessible = getAccessibleModes(userRole);
    if (!accessible.includes(nextMode)) {
      toast.error('权限不足', { description: '您只能访问 Roys 助教系统' });
      return;
    }
    setMode(nextMode);
    const nextConfig = MODE_CONFIG[nextMode];
    toast.success(`已切换到 ${nextConfig.label}`, {
      description: nextConfig.description,
      duration: 3000,
    });
  };

  const getHeaderTitle = () => {
    switch (activeView) {
      case 'dashboard':
        return '欢迎大厅';
      case 'knowledge':
        return selectedKnowledgeBase ? '知识库详情' : '知识库管理';
      case 'chat':
        return '对话答疑';
      case 'qa':
        return '破题结界';
      case 'retrieval':
        return '检索测试';
      case 'debate':
        return '辩论竞技场';
      case 'settings':
        return '设置';
      case 'admin':
        return '管理面板';
      default:
        return '欢迎大厅';
    }
  };

  const handleNavigate = (view: ViewId) => {
    if (view === 'admin') {
      if (!canAccessAdminPanel(userRole)) {
        toast.error('权限不足', { description: '需要管理员权限' });
        return;
      }
      setActiveView('admin');
      setSelectedKnowledgeBase(null);
      setSelectedDocument(null);
      return;
    }
    if (!MODE_CONFIG[mode].allowedViews.includes(view)) {
      return;
    }
    setActiveView(view);
    setSelectedKnowledgeBase(null);
    setSelectedDocument(null);
  };

  const handleViewKnowledgeBaseDetail = (collectionId: string) => {
    setSelectedKnowledgeBase(collectionId);
  };

  const handleBackToKnowledgeBase = () => {
    setSelectedKnowledgeBase(null);
    setSelectedDocument(null);
  };

  const handleViewDocument = (fileId: string) => {
    setSelectedDocument(fileId);
  };

  const handleBackToDetail = () => {
    setSelectedDocument(null);
  };

  const renderContent = () => {
    if (activeView === 'admin') {
      return <AdminPanel />;
    }
    if (activeView === 'knowledge') {
      if (selectedDocument) {
        return (
          <DocumentViewer
            fileId={selectedDocument}
            collectionId={selectedKnowledgeBase || ''}
            onBack={handleBackToDetail}
            readOnly={MODE_CONFIG[mode].readOnly}
          />
        );
      }
      if (selectedKnowledgeBase) {
        return (
          <KnowledgeBaseDetail
            collectionId={selectedKnowledgeBase}
            onBack={handleBackToKnowledgeBase}
            onViewDocument={handleViewDocument}
            mode={mode}
          />
        );
      }
      return <KnowledgeBase onViewDetail={handleViewKnowledgeBaseDetail} mode={mode} />;
    }

    switch (activeView) {
      case 'dashboard':
        return <Dashboard onNavigate={handleNavigate} mode={mode} />;
      case 'chat':
        return (
          <Chat
            mode={mode}
            isHistoryOpen={isSecondarySidebarOpen}
            onToggleHistory={() => setIsSecondarySidebarOpen((prev) => !prev)}
            onSetHistoryOpen={setIsSecondarySidebarOpen}
          />
        );
      case 'qa':
        return (
          <QuestionSolver
            mode={mode}
            isHistoryOpen={isSecondarySidebarOpen}
            onToggleHistory={() => setIsSecondarySidebarOpen((prev) => !prev)}
            onSetHistoryOpen={setIsSecondarySidebarOpen}
          />
        );
      case 'retrieval':
        return <RetrievalTest />;
      case 'debate':
        return (
          <DebateArena
            mode={mode}
            isHistoryOpen={isSecondarySidebarOpen}
            onToggleHistory={() => setIsSecondarySidebarOpen((prev) => !prev)}
            onSetHistoryOpen={setIsSecondarySidebarOpen}
          />
        );
      case 'settings':
        return <Settings mode={mode} />;
      default:
        return <Dashboard onNavigate={handleNavigate} mode={mode} />;
    }
  };

  const usesDarkTheme = useMemo(() => isDarkThemeMode(mode), [mode]);
  const sidebarWidth = isSidebarCollapsed ? 88 : 260;

  const themeClass = useMemo(() => {
    if (mode === 'v2') return 'theme-v2';
    if (visualTheme === 'warm') return 'theme-warm';
    if (mode === 'roys') return 'theme-roys';
    return '';
  }, [mode, visualTheme]);

  const showParticles = usesDarkTheme && visualTheme === 'dark';
  const showWarmBg = visualTheme === 'warm' && mode !== 'v2';
  const isLightTheme = !usesDarkTheme || visualTheme === 'warm';

  return (
    <div className={`min-h-screen ${themeClass}`}>
      {/* Animated Background - 仅在暗色主题显示 (Particle Network) */}
      {showParticles && <ParticleBackground />}

      {/* 亮色主题背景 (v2) */}
      {!usesDarkTheme && (
        <div className="fixed inset-0 pointer-events-none" style={{ zIndex: 0 }}>
          <div className="absolute inset-0 bg-gradient-to-br from-gray-50 via-white to-blue-50/30" />
        </div>
      )}

      {/* 暖色主题背景 */}
      {showWarmBg && (
        <div className="fixed inset-0 pointer-events-none" style={{ zIndex: 0 }}>
          <div className="absolute inset-0" style={{ background: 'linear-gradient(135deg, #FDFBF7 0%, #F5F0E8 50%, #EDE6DA 100%)' }} />
        </div>
      )}

      <Sidebar
        activeView={activeView}
        onNavigate={handleNavigate}
        mode={mode}
        onSelectMode={handleSelectMode}
        userRole={userRole}
        isCollapsed={isSidebarCollapsed}
        onToggleCollapse={() => setIsSidebarCollapsed((prev) => !prev)}
        visualTheme={visualTheme}
        onToggleTheme={toggleVisualTheme}
      />

      <div
        className="transition-[margin] duration-300 ease-in-out"
        style={{ marginLeft: `${sidebarWidth}px`, position: 'relative', zIndex: 10, minHeight: '100vh' }}
      >
        <Header title={getHeaderTitle()} />

        <main 
          className={(activeView === 'chat' || activeView === 'qa' || activeView === 'debate' || (activeView === 'knowledge' && selectedDocument)) ? 'h-screen overflow-hidden' : ''}
          style={{
            paddingTop: (activeView === 'chat' || activeView === 'qa' || activeView === 'debate' || (activeView === 'knowledge' && selectedDocument)) ? '64px' : '80px',
            minHeight: (activeView === 'chat' || activeView === 'qa' || activeView === 'debate' || (activeView === 'knowledge' && selectedDocument)) ? undefined : 'calc(100vh - 64px)'
          }}
        >
          <div className={(activeView === 'chat' || activeView === 'qa' || activeView === 'debate' || (activeView === 'knowledge' && selectedDocument)) ? 'h-full' : 'max-w-[1440px] mx-auto p-6'}>
            <AnimatePresence mode="sync">
              <motion.div
                key={activeView + (selectedKnowledgeBase || '') + (selectedDocument || '')}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
                transition={{ duration: 0.3, ease: "easeOut" }}
                className={(activeView === 'chat' || activeView === 'qa' || activeView === 'debate' || (activeView === 'knowledge' && selectedDocument)) ? 'h-full' : ''}
              >
                {renderContent()}
              </motion.div>
            </AnimatePresence>
          </div>
        </main>
      </div>

      <Toaster
        theme={isLightTheme ? 'light' : 'dark'}
        position="top-right"
        toastOptions={{
          style: isLightTheme ? {
            background: visualTheme === 'warm' ? '#FDFBF7' : 'white',
            color: visualTheme === 'warm' ? '#2C1810' : '#1e293b',
            border: visualTheme === 'warm' ? '1px solid rgba(180, 160, 130, 0.3)' : '1px solid #e2e8f0',
          } : {
            background: 'rgba(22, 32, 50, 0.95)',
            color: '#D4DCE8',
            border: '1px solid rgba(56, 189, 248, 0.2)',
          },
        }}
      />
    </div>
  );
}
