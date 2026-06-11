import { ArrowRight, Bot, TrendingUp, TrendingDown, BookOpen, MessageSquare, Lightbulb, Users } from 'lucide-react';
import { motion } from 'motion/react';
import { useState, useEffect, useCallback } from 'react';
import { UploadDialog, type UploadConfig } from './UploadDialog';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';
import { MODE_CONFIG, ModeId, ViewId } from '../src/modes';

interface DashboardProps {
  onNavigate?: (view: ViewId) => void;
  mode: ModeId;
}

interface StatsCard {
  id: number;
  label: string;
  value: string;
  unit: string;
  icon: string;
  gradient: string;
  trend: string;
  trendUp: boolean;
}

interface CollectionStats {
  collection_name: string;
  total_documents?: number;
  total_chunks?: number;
}

interface ConversationSummary {
  id: string;
  title: string;
  knowledgeBase: string;
  timestamp: string;
}

interface StoredChatSession {
  id: string;
  title: string;
  knowledgeBaseName: string;
  updatedAt: string;
}

interface StatsApiResponse {
  status: 'success' | 'error';
  data: {
    collections?: CollectionStats[];
  };
}

export function Dashboard({ onNavigate, mode }: DashboardProps) {
  const [showUploadDialog, setShowUploadDialog] = useState(false);
  const modeConfig = MODE_CONFIG[mode];
  const isReadOnly = modeConfig.readOnly;
  const [stats, setStats] = useState<StatsCard[]>([
    { id: 1, label: '知识库', value: '0', unit: '个', icon: '📚', gradient: 'from-[#38BDF8] to-[#0EA5E9]', trend: '+0', trendUp: true },
    { id: 2, label: '文档数', value: '0', unit: '', icon: '📄', gradient: 'from-[#34D399] to-[#10B981]', trend: '+0', trendUp: true },
    { id: 3, label: '查询次数', value: '0', unit: '', icon: '🔍', gradient: 'from-[#8b5cf6] to-[#6366f1]', trend: '+0%', trendUp: true },
    { id: 4, label: '响应时间', value: '0', unit: 'ms', icon: '⚡', gradient: 'from-[#ffb800] to-[#ff8c00]', trend: '-0ms', trendUp: true },
  ]);

  // 从Milvus API获取统计数据
  const fetchStats = useCallback(async () => {
    try {
      const response = await authFetch(`${config.milvusApiUrl}/stats/all`);
      const result = (await response.json()) as StatsApiResponse;

      if (result.status === 'success') {
        const collections = result.data.collections ?? [];

        const totalCollections = collections.length;
        const totalDocuments = collections.reduce(
          (sum: number, col: CollectionStats) => sum + (col.total_documents ?? 0),
          0
        );
        const totalChunks = collections.reduce(
          (sum: number, col: CollectionStats) => sum + (col.total_chunks ?? 0),
          0
        );
        
        setStats([
          { id: 1, label: '知识库', value: String(totalCollections), unit: '个', icon: '📚', gradient: 'from-[#38BDF8] to-[#0EA5E9]', trend: '+0', trendUp: true },
          { id: 2, label: '文档数', value: String(totalDocuments), unit: '', icon: '📄', gradient: 'from-[#34D399] to-[#10B981]', trend: '+0', trendUp: true },
          { id: 3, label: 'Chunk数', value: String(totalChunks), unit: '', icon: '🔍', gradient: 'from-[#8b5cf6] to-[#6366f1]', trend: '+0', trendUp: true },
          { id: 4, label: '响应时间', value: '142', unit: 'ms', icon: '⚡', gradient: 'from-[#ffb800] to-[#ff8c00]', trend: '-8ms', trendUp: true },
        ]);
      }
    } catch (error) {
      console.error('获取统计数据失败:', error);
    }
  }, []);

  useEffect(() => {
    fetchStats();
    // 每30秒刷新一次数据
    const interval = setInterval(fetchStats, 30000);
    return () => clearInterval(interval);
  }, [fetchStats]);

  const handleUpload = (files: File[], kbId: string, uploadConfig: UploadConfig) => {
    console.log('Uploading files:', files, 'to KB:', kbId, 'with config:', uploadConfig);
    // 上传完成后刷新统计数据
    fetchStats();
  };

  const [conversations, setConversations] = useState<ConversationSummary[]>([]);

  // 从localStorage加载最近对话
  useEffect(() => {
    const loadRecentConversations = () => {
      try {
        const savedSessions = localStorage.getItem('chat_sessions');
        if (savedSessions) {
          const sessions: StoredChatSession[] = JSON.parse(savedSessions);
          // 只显示最近3个对话
          const recentSessions = sessions.slice(0, 3).map((session) => ({
            id: session.id,
            title: session.title,
            knowledgeBase: session.knowledgeBaseName,
            timestamp: formatTimestamp(session.updatedAt),
          }));
          setConversations(recentSessions);
        }
      } catch (error) {
        console.error('加载最近对话失败:', error);
      }
    };

    loadRecentConversations();
    // 每5秒检查一次更新
    const interval = setInterval(loadRecentConversations, 5000);
    return () => clearInterval(interval);
  }, []);

  // 格式化时间戳
  const formatTimestamp = (isoString: string) => {
    const date = new Date(isoString);
    const now = new Date();
    const diff = now.getTime() - date.getTime();
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);

    if (minutes < 60) return `${minutes}分钟前`;
    if (hours < 24) return `${hours}小时前`;
    return `${days}天前`;
  };

  const quickActions = [
    { id: 1, label: '上传文档', icon: '📤', variant: 'primary' },
    { id: 2, label: '开始对话', icon: '💬', variant: 'outline' },
    { id: 3, label: '测试检索', icon: '🔍', variant: 'secondary' },
  ];

  // 支持学科数据
  const supportedSubjects = [
    {
      category: 'CIE IGCSE',
      subjects: ['经济', '化学'],
      gradient: 'from-[#38BDF8] to-[#0EA5E9]',
    },
    {
      category: 'AP',
      subjects: ['微积分 AB & BC', '统计', '物理 C 力学 & 电磁学', '微观 & 宏观经济'],
      gradient: 'from-[#34D399] to-[#10B981]',
    },
    {
      category: 'CIE A-Level',
      subjects: ['普通数学 P1, P2, P3', '普通数学 S1, S2', '高数 FP1, FP2', '高数 FPS', 'AS & A2 物理', 'AS & A2 经济'],
      gradient: 'from-[#8b5cf6] to-[#6366f1]',
    },
    {
      category: 'International Edexcel A-Level',
      subjects: ['AS & A2 经济'],
      gradient: 'from-[#ffb800] to-[#ff8c00]',
    },
  ];

  // Roys 模式 - 欢迎大厅
  if (mode === 'roys') {
    return (
      <div className="space-y-6">
        {/* 欢迎标题区 */}
        <motion.div
          className="glass gradient-border rounded-2xl p-8"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <div className="mb-4">
            <h1 className="text-3xl font-bold text-gradient">欢迎来到 Roys Legion</h1>
            <p className="text-[#94A3B8] mt-1">乐亦思国际学科助教军团</p>
          </div>
          <p className="text-[#CBD5E1] leading-relaxed">
            Roys Legion 是一款专为国际课程学生打造的智能学科助教系统，
            为您提供精准的知识点答疑、真题解析和深度学科辅导，
            助力您在国际课程学习中取得优异成绩。
          </p>
        </motion.div>

        {/* 支持学科列表 */}
        <motion.div
          className="glass gradient-border rounded-2xl p-6"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.1 }}
        >
          <div className="flex items-center gap-3 mb-6">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center">
              <BookOpen size={20} className="text-[#0F172A]" />
            </div>
            <div>
              <h3 className="text-[#E2E8F0] text-lg font-medium">已支持学科</h3>
              <p className="text-[#64748B] text-sm">覆盖主流国际课程体系</p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {supportedSubjects.map((item, index) => (
              <motion.div
                key={item.category}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.3, delay: 0.2 + index * 0.1 }}
                className="glass rounded-xl p-4 border border-[rgba(56,189,248,0.2)] hover:border-[rgba(56,189,248,0.4)] transition-all group"
              >
                <div className="flex items-center gap-2 mb-3">
                  <div className={`w-8 h-8 rounded-lg bg-gradient-to-br ${item.gradient} flex items-center justify-center text-sm font-semibold text-[#0F172A]`}>
                    {item.category.split(' ')[0].charAt(0)}
                  </div>
                  <span className="text-[#E2E8F0] font-medium">{item.category}</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {item.subjects.map((subject) => (
                    <span
                      key={subject}
                      className="px-3 py-1 text-xs rounded-full bg-[rgba(56,189,248,0.1)] text-[#38BDF8] border border-[rgba(56,189,248,0.2)]"
                    >
                      {subject}
                    </span>
                  ))}
                </div>
              </motion.div>
            ))}
          </div>
        </motion.div>

        {/* 快速入口 */}
        <motion.div
          className="glass gradient-border rounded-2xl p-6"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.3 }}
        >
          <h3 className="text-[#E2E8F0] text-lg font-medium mb-6">开始学习</h3>
          <div className="grid grid-cols-3 gap-4">
            <motion.button
              onClick={() => onNavigate && onNavigate('chat')}
              whileHover={{ scale: 1.02, y: -2 }}
              whileTap={{ scale: 0.98 }}
              className="p-6 rounded-xl glass border border-[rgba(56,189,248,0.2)] hover:border-[#38BDF8] transition-all group text-left"
            >
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center mb-4 group-hover:shadow-[0_0_20px_rgba(56,189,248,0.4)] transition-shadow">
                <MessageSquare size={24} className="text-[#0F172A]" />
              </div>
              <h4 className="text-[#E2E8F0] font-medium mb-1">智能对话</h4>
              <p className="text-[#64748B] text-sm">与 AI 助教进行学科问答</p>
            </motion.button>

            <motion.button
              onClick={() => onNavigate && onNavigate('qa')}
              whileHover={{ scale: 1.02, y: -2 }}
              whileTap={{ scale: 0.98 }}
              className="p-6 rounded-xl glass border border-[rgba(56,189,248,0.2)] hover:border-[#38BDF8] transition-all group text-left"
            >
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[#34D399] to-[#10B981] flex items-center justify-center mb-4 group-hover:shadow-[0_0_20px_rgba(52,211,153,0.4)] transition-shadow">
                <Lightbulb size={24} className="text-[#0F172A]" />
              </div>
              <h4 className="text-[#E2E8F0] font-medium mb-1">破题结界</h4>
              <p className="text-[#64748B] text-sm">真题解析与答题指导</p>
            </motion.button>

            <motion.button
              onClick={() => onNavigate && onNavigate('debate')}
              whileHover={{ scale: 1.02, y: -2 }}
              whileTap={{ scale: 0.98 }}
              className="p-6 rounded-xl glass border border-[rgba(56,189,248,0.2)] hover:border-[#38BDF8] transition-all group text-left"
            >
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[#8b5cf6] to-[#6366f1] flex items-center justify-center mb-4 group-hover:shadow-[0_0_20px_rgba(139,92,246,0.4)] transition-shadow">
                <Users size={24} className="text-white" />
              </div>
              <h4 className="text-[#E2E8F0] font-medium mb-1">辩论竞技场</h4>
              <p className="text-[#64748B] text-sm">多智能体思辨与讨论</p>
            </motion.button>
          </div>
        </motion.div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Statistics Cards */}
      <div className="grid grid-cols-4 gap-4">
        {stats.map((stat, index) => (
          <motion.div
            key={stat.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: index * 0.1 }}
            whileHover={{ y: -5, transition: { duration: 0.2 } }}
            className="glass gradient-border rounded-2xl p-6 group cursor-pointer relative overflow-hidden"
          >
            {/* Background Shimmer Effect */}
            <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500">
              <div className="absolute inset-0 bg-gradient-to-r from-transparent via-[rgba(56,189,248,0.1)] to-transparent shimmer" />
            </div>

            <div className="relative z-10">
              <div className="flex items-start justify-between mb-4">
                <div className={`w-12 h-12 rounded-xl bg-gradient-to-br ${stat.gradient} flex items-center justify-center text-2xl shadow-lg group-hover:scale-110 transition-transform duration-300`}>
                  {stat.icon}
                </div>
                <div className={`flex items-center gap-1 px-2 py-1 rounded-lg ${stat.trendUp ? 'bg-[rgba(52,211,153,0.1)] text-[#34D399]' : 'bg-[rgba(255,59,92,0.1)] text-[#ff3b5c]'}`}>
                  {stat.trendUp ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                  <span className="text-xs">{stat.trend}</span>
                </div>
              </div>
              
              <div className="flex items-baseline gap-1 mb-2">
                <span className="text-4xl text-[#E2E8F0]">{stat.value}</span>
                {stat.unit && <span className="text-lg text-[#94A3B8]">{stat.unit}</span>}
              </div>
              
              <div className="text-[#94A3B8]">{stat.label}</div>
            </div>

            {/* Glow effect on hover */}
            <div className={`absolute inset-0 rounded-2xl opacity-0 group-hover:opacity-100 transition-opacity duration-300 bg-gradient-to-br ${stat.gradient} blur-xl -z-10`} style={{ filter: 'blur(20px)' }} />
          </motion.div>
        ))}
      </div>

      {/* Recent Conversations */}
      <motion.div 
        className="glass gradient-border rounded-2xl p-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: 0.5 }}
      >
        <div className="flex items-center justify-between mb-6">
          <h3 className="text-[#E2E8F0]">最近对话</h3>
          <motion.button
            onClick={() => onNavigate && onNavigate('chat')}
            className="text-[#38BDF8] flex items-center gap-1 hover:gap-2 transition-all duration-300 group"
            whileHover={{ scale: 1.05 }}
          >
            查看全部
            <ArrowRight size={16} className="group-hover:translate-x-1 transition-transform" />
          </motion.button>
        </div>

        <div className="space-y-2">
          {conversations.length === 0 ? (
            <div className="text-center py-12 text-[#94A3B8]">
              <Bot size={48} className="mx-auto mb-4 opacity-50" />
              <p>暂无对话记录</p>
              <motion.button
                onClick={() => onNavigate && onNavigate('chat')}
                className="mt-4 px-4 py-2 border border-[#38BDF8] text-[#38BDF8] rounded-lg hover:bg-[rgba(56,189,248,0.1)] transition-all"
                whileHover={{ scale: 1.05 }}
              >
                开始第一次对话
              </motion.button>
            </div>
          ) : (
            conversations.map((conv, index) => (
            <motion.div
              key={conv.id}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, delay: 0.6 + index * 0.1 }}
              whileHover={{ x: 5, transition: { duration: 0.2 } }}
              className="flex items-center gap-4 p-4 rounded-xl hover:bg-[rgba(56,189,248,0.05)] cursor-pointer transition-all duration-300 border border-transparent hover:border-[rgba(56,189,248,0.2)] group"
            >
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center flex-shrink-0 shadow-lg group-hover:shadow-[0_0_20px_rgba(56,189,248,0.4)] transition-shadow">
                <Bot size={20} className="text-[#0F172A]" />
              </div>
              
              <div className="flex-1 min-w-0">
                <div className="text-[#E2E8F0] mb-1 group-hover:text-[#38BDF8] transition-colors">{conv.title}</div>
                <div className="flex items-center gap-2">
                  <span className="inline-flex items-center px-3 py-1 rounded-lg text-xs bg-[rgba(56,189,248,0.1)] text-[#38BDF8] border border-[rgba(56,189,248,0.2)]">
                    {conv.knowledgeBase}
                  </span>
                </div>
              </div>
              
              <div className="text-[#94A3B8] text-sm flex-shrink-0">
                {conv.timestamp}
              </div>
            </motion.div>
          )))}
        </div>
      </motion.div>

      {/* Quick Actions */}
      {!isReadOnly ? (
        <>
          <motion.div 
            className="glass gradient-border rounded-2xl p-6"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.8 }}
          >
            <h3 className="text-[#E2E8F0] mb-6">快速操作</h3>
            
            <div className="flex gap-4">
              {quickActions.map((action, index) => (
                <motion.button
                  key={action.id}
                  onClick={() => {
                    if (action.id === 1) {
                      setShowUploadDialog(true);
                    } else if (action.id === 2 && onNavigate) {
                      onNavigate('chat');
                    }
                  }}
                  initial={{ opacity: 0, scale: 0.9 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ duration: 0.3, delay: 0.9 + index * 0.1 }}
                  whileHover={{ scale: 1.05, y: -2 }}
                  whileTap={{ scale: 0.95 }}
                  className={`flex-1 h-16 rounded-xl flex items-center justify-center gap-3 transition-all duration-300 relative overflow-hidden group ${
                    action.variant === 'primary'
                      ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] shadow-[0_0_20px_rgba(56,189,248,0.4)] hover:shadow-[0_0_30px_rgba(56,189,248,0.6)]'
                      : action.variant === 'outline'
                      ? 'border-2 border-[#38BDF8] text-[#38BDF8] hover:bg-[rgba(56,189,248,0.1)]'
                      : 'bg-[rgba(56,189,248,0.1)] text-[#38BDF8] border border-[rgba(56,189,248,0.2)] hover:bg-[rgba(56,189,248,0.2)]'
                  }`}
                >
                  {action.variant === 'primary' && (
                    <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
                  )}
                  <span className="text-2xl relative z-10">{action.icon}</span>
                  <span className="relative z-10">{action.label}</span>
                </motion.button>
              ))}
            </div>
          </motion.div>

          <UploadDialog
            isOpen={showUploadDialog}
            onClose={() => setShowUploadDialog(false)}
            onUpload={handleUpload}
            mode={mode}
          />
        </>
      ) : (
        <motion.div 
          className="glass gradient-border rounded-2xl p-6"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.8 }}
        >
          <h3 className="text-[#E2E8F0] mb-2">操作受限</h3>
          <p className="text-[#94A3B8] text-sm">
            当前模式为「Roys乐亦思国际学科助教军团」，仅支持查看仪表盘和对话数据。如需管理知识库，请切换到管理员模式。
          </p>
        </motion.div>
      )}
    </div>
  );
}
