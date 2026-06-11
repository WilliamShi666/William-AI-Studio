import { Search, Plus, Trash2, Database } from 'lucide-react';
import { useState, useEffect, useCallback } from 'react';
import { motion } from 'motion/react';
import { ConfirmDialog } from './ConfirmDialog';
import { Toast } from './Toast';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';
import { MODE_CONFIG, ModeId, isModeUsingV2Data } from '../src/modes';

interface KnowledgeBaseProps {
  onViewDetail: (collectionId: string) => void;
  mode: ModeId;
}

interface KnowledgeBaseData {
  id: number;
  collection_id: string;  // Milvus collection ID
  name: string;  // 原始名称
  displayName: string;
  isV2: boolean;
  icon: string;
  iconBg: string;
  documents: number;
  chunks: number | string;
  updated: string;
  storageUsed: number;
}

interface CollectionStats {
  collection_id: string;
  collection_name: string;
  total_documents?: number;
  total_chunks?: number;
  last_updated?: string;
}

interface StatsApiResponse {
  status: 'success' | 'error';
  data: {
    collections?: CollectionStats[];
  };
}

export function KnowledgeBase({ onViewDetail, mode }: KnowledgeBaseProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseData[]>([]);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newKbName, setNewKbName] = useState('');
  const [loading, setLoading] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<{ show: boolean; id: string; name: string }>({
    show: false,
    id: '',
    name: '',
  });
  const usesV2Data = isModeUsingV2Data(mode);
  const modeConfig = MODE_CONFIG[mode];
  const isReadOnly = modeConfig.readOnly;
  const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'error' | 'info' | 'warning' }>({
    show: false,
    message: '',
    type: 'info',
  });

  // 从Milvus API获取知识库列表
  const fetchKnowledgeBases = useCallback(async () => {
    setLoading(true);
    try {
      const response = await authFetch(`${config.milvusApiUrl}/stats/all`);
      const result = (await response.json()) as StatsApiResponse;

      if (result.status === 'success') {
        const collections = result.data.collections ?? [];

        // 图标映射
        const icons = ['📘', '📗', '📙', '📕', '📔', '📓'];
        const iconBgs = ['bg-blue-100', 'bg-green-100', 'bg-orange-100', 'bg-red-100', 'bg-purple-100', 'bg-pink-100'];

        const kbData = collections.map((col: CollectionStats, index: number) => {
          // 格式化更新时间
          let updated = '未知';
          if (col.last_updated) {
            const date = new Date(col.last_updated);
            const now = new Date();
            const diffMs = now.getTime() - date.getTime();
            const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
            const diffDays = Math.floor(diffHours / 24);

            if (diffHours < 1) {
              updated = '刚刚';
            } else if (diffHours < 24) {
              updated = `${diffHours}小时前`;
            } else if (diffDays < 7) {
              updated = `${diffDays}天前`;
            } else {
              updated = date.toLocaleDateString('zh-CN');
            }
          }

          const isV2Collection = col.collection_name.endsWith('_v2');
          const displayName = isV2Collection ? col.collection_name.slice(0, -3) : col.collection_name;

          return {
            id: index + 1,
            collection_id: col.collection_id,
            name: col.collection_name,
            displayName,
            isV2: isV2Collection,
            icon: icons[index % icons.length],
            iconBg: iconBgs[index % iconBgs.length],
            documents: col.total_documents ?? 0,
            chunks: col.total_chunks ?? 0,
            updated: updated,
            storageUsed: Math.min(95, Math.floor((col.total_chunks ?? 0) / 100)), // 简单的存储使用率估算
          };
        });

        setKnowledgeBases(kbData);
      }
    } catch (error) {
      console.error('获取知识库列表失败:', error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchKnowledgeBases();
  }, [fetchKnowledgeBases, mode]);

  const showToast = (message: string, type: 'success' | 'error' | 'info' | 'warning' = 'info') => {
    setToast({ show: true, message, type });
  };

  const handleCreateKB = async () => {
    if (!newKbName.trim()) {
      showToast('请输入知识库名称', 'warning');
      return;
    }

    if (isReadOnly) {
      showToast('当前模式不支持创建知识库', 'warning');
      return;
    }

    try {
      // ✅ V2模式：自动添加 _v2 后缀
      const actualKbName = usesV2Data ? `${newKbName}_v2` : newKbName;
      
      const response = await authFetch(`${config.milvusApiUrl}/knowledge_base/create?display_name=${encodeURIComponent(actualKbName)}`, {
        method: 'POST',
      });
      const result = await response.json();

      if (result.status === 'success') {
        showToast(result.message, 'success');
        setShowCreateDialog(false);
        setNewKbName('');
        // 刷新列表
        await fetchKnowledgeBases();
      } else {
        showToast(result.message || '创建知识库失败', 'error');
      }
    } catch (error) {
      console.error('创建知识库失败:', error);
      showToast('创建知识库失败: ' + (error instanceof Error ? error.message : String(error)), 'error');
    }
  };

  const confirmDelete = (collectionId: string, displayName: string) => {
    if (isReadOnly) {
      showToast('当前模式不支持删除知识库', 'warning');
      return;
    }
    setDeleteConfirm({ show: true, id: collectionId, name: displayName });
  };

  const handleDeleteKB = async () => {
    try {
      const response = await authFetch(`${config.milvusApiUrl}/knowledge_base/delete`, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ collection_id: deleteConfirm.id }),
      });
      const result = await response.json();

      if (result.status === 'success') {
        showToast(result.message, 'success');
        // 刷新列表
        await fetchKnowledgeBases();
      } else {
        showToast(result.message || '删除知识库失败', 'error');
      }
    } catch (error) {
      console.error('删除知识库失败:', error);
      showToast('删除知识库失败', 'error');
    }
  };

  // 过滤知识库
  const filteredKBs = knowledgeBases.filter(kb => {
    const query = searchQuery.toLowerCase();
    return (
      kb.name.toLowerCase().includes(query) ||
      kb.displayName.toLowerCase().includes(query)
    );
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <motion.div 
        className="flex items-center justify-between"
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center shadow-lg">
            <Database size={24} className="text-[#0F172A]" />
          </div>
          <h2 className="text-gradient">知识库管理</h2>
        </div>
        {!isReadOnly && (
          <motion.button
            onClick={() => setShowCreateDialog(true)}
            className="px-6 py-3 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl hover:shadow-[0_0_30px_rgba(56,189,248,0.6)] transition-all flex items-center gap-2 relative overflow-hidden group"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
            <Plus size={18} className="relative z-10" />
            <span className="relative z-10">新建知识库</span>
          </motion.button>
        )}
      </motion.div>

      {/* Stats Summary */}
      {!loading && knowledgeBases.length > 0 && (
        <motion.div
          className="flex items-center gap-6 glass rounded-xl p-4 border border-[rgba(56,189,248,0.2)]"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
        >
          <div className="flex items-center gap-2">
            <span className="text-[#94A3B8] text-sm">总计:</span>
            <span className="text-[#E2E8F0] font-medium">{knowledgeBases.length} 个知识库</span>
          </div>
          <div className="w-px h-4 bg-[rgba(56,189,248,0.2)]" />
          <div className="flex items-center gap-2">
            <span className="text-[#94A3B8] text-sm">文档:</span>
            <span className="text-[#38BDF8] font-medium">
              {knowledgeBases.reduce((sum, kb) => sum + kb.documents, 0)} 个
            </span>
          </div>
          <div className="w-px h-4 bg-[rgba(56,189,248,0.2)]" />
          <div className="flex items-center gap-2">
            <span className="text-[#94A3B8] text-sm">Chunks:</span>
            <span className="text-[#34D399] font-medium">
              {knowledgeBases.reduce((sum, kb) => sum + (typeof kb.chunks === 'number' ? kb.chunks : 0), 0)} 个
            </span>
          </div>
        </motion.div>
      )}

      {/* Search Bar */}
      <motion.div
        className="relative"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
      >
        <Search className="absolute left-4 top-1/2 transform -translate-y-1/2 text-[#38BDF8]" size={20} />
        <input
          type="text"
          placeholder="🔍 搜索知识库..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full h-14 pl-12 pr-4 glass-strong rounded-xl border border-[rgba(56,189,248,0.2)] focus:outline-none focus:ring-2 focus:ring-[#38BDF8] focus:border-[#38BDF8] text-[#E2E8F0] placeholder-[#94A3B8] transition-all duration-300"
        />
      </motion.div>

      {/* Loading State */}
      {loading && (
        <div className="text-center text-[#94A3B8] py-12">
          <div className="inline-block animate-spin rounded-full h-12 w-12 border-b-2 border-[#38BDF8]"></div>
          <p className="mt-4">加载中...</p>
        </div>
      )}

      {/* Empty State */}
      {!loading && filteredKBs.length === 0 && (
        <div className="text-center text-[#94A3B8] py-12">
          <Database size={48} className="mx-auto mb-4 opacity-50" />
          <p>暂无知识库</p>
        </div>
      )}

      {/* Knowledge Base Cards */}
      <div className="space-y-4">
        {!loading && filteredKBs.map((kb, index) => (
          <motion.div
            key={kb.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.2 + index * 0.1 }}
            whileHover={{ y: -4, transition: { duration: 0.2 } }}
            className="glass gradient-border rounded-2xl p-6 hover:shadow-[0_0_30px_rgba(56,189,248,0.3)] transition-all cursor-pointer group relative overflow-hidden"
          >
            {/* Hover shimmer effect */}
            <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500">
              <div className="absolute inset-0 bg-gradient-to-r from-transparent via-[rgba(56,189,248,0.1)] to-transparent shimmer" />
            </div>

            <div className="flex items-start gap-4 relative z-10">
              {/* Icon */}
              <div className={`w-16 h-16 rounded-2xl bg-gradient-to-br ${kb.iconBg === 'bg-blue-100' ? 'from-[#38BDF8] to-[#0EA5E9]' : kb.iconBg === 'bg-green-100' ? 'from-[#34D399] to-[#10B981]' : 'from-[#ffb800] to-[#ff8c00]'} flex items-center justify-center text-3xl flex-shrink-0 shadow-lg group-hover:scale-110 group-hover:rotate-3 transition-transform duration-300`}>
                {kb.icon}
              </div>

              {/* Content */}
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-1">
                  <h3 className="text-[#E2E8F0] group-hover:text-[#38BDF8] transition-colors text-lg">{kb.displayName}</h3>
                  <span className={`px-2 py-0.5 rounded-full text-[11px] border ${kb.isV2 ? 'border-blue-400 text-blue-300 bg-blue-500/10' : 'border-emerald-400 text-emerald-300 bg-emerald-500/10'}`}>
                    {kb.isV2 ? 'OCR v2.0.0' : 'VLM v1.0.0'}
                  </span>
                </div>
                <p className="text-xs text-[#64748B] mb-3">内部名称：{kb.name}</p>

                <div className="text-[#94A3B8] mb-4 text-sm flex items-center gap-3">
                  <span className="px-3 py-1 rounded-lg bg-[rgba(56,189,248,0.1)] text-[#38BDF8] border border-[rgba(56,189,248,0.2)]">
                    {kb.documents}个文档
                  </span>
                  <span className="px-3 py-1 rounded-lg bg-[rgba(52,211,153,0.1)] text-[#34D399] border border-[rgba(52,211,153,0.2)]">
                    {kb.chunks} chunks
                  </span>
                  <span>更新: {kb.updated}</span>
                </div>

                {/* Progress Bar */}
                <div className="mb-4">
                  <div className="flex items-center justify-between text-xs text-[#94A3B8] mb-2">
                    <span>存储使用</span>
                    <span className="text-[#38BDF8]">{kb.storageUsed}%</span>
                  </div>
                  <div className="h-2 bg-[rgba(30,41,59,0.6)] rounded-full overflow-hidden border border-[rgba(56,189,248,0.2)]">
                    <motion.div
                      initial={{ width: 0 }}
                      animate={{ width: `${kb.storageUsed}%` }}
                      transition={{ duration: 1, delay: 0.5 + index * 0.1 }}
                      className="h-full bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] relative overflow-hidden"
                    >
                      <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-30 shimmer" />
                    </motion.div>
                  </div>
                </div>
              </div>

              {/* Actions */}
              <div className="flex gap-2">
                <motion.button
                  onClick={() => onViewDetail(kb.collection_id)}
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                  className="px-6 py-2.5 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl hover:shadow-[0_0_20px_rgba(56,189,248,0.5)] transition-all"
                >
                  进入
                </motion.button>
                {!isReadOnly && (
                  <motion.button
                    onClick={(e) => {
                      e.stopPropagation();
                      confirmDelete(kb.collection_id, kb.name);
                    }}
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    className="px-4 py-2.5 border-2 border-[#ff3b5c] text-[#ff3b5c] rounded-xl hover:bg-[rgba(255,59,92,0.1)] transition-all"
                  >
                    <Trash2 size={18} />
                  </motion.button>
                )}
              </div>
            </div>
          </motion.div>
        ))}
      </div>

      {/* Create Knowledge Base Dialog */}
      {!isReadOnly && showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <motion.div
            initial={{ opacity: 0, scale: 0.9, y: 20 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            transition={{ duration: 0.3, ease: 'easeOut' }}
            className="glass gradient-border rounded-2xl p-8 w-full max-w-md mx-4 relative overflow-hidden"
          >
            {/* Background shimmer */}
            <div className="absolute inset-0 opacity-5">
              <div className="absolute inset-0 bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] blur-3xl" />
            </div>

            <div className="relative z-10">
              <h3 className="text-2xl mb-6 text-[#E2E8F0]">新建知识库</h3>

              <div className="mb-6">
                <label className="block text-[#94A3B8] mb-2">知识库名称</label>
                <input
                  type="text"
                  value={newKbName}
                  onChange={(e) => setNewKbName(e.target.value)}
                  placeholder="请输入知识库名称（支持中文）"
                  className="w-full px-4 py-3 glass-strong rounded-xl border border-[rgba(56,189,248,0.2)] focus:outline-none focus:ring-2 focus:ring-[#38BDF8] focus:border-[#38BDF8] text-[#E2E8F0] placeholder-[#94A3B8] transition-all"
                  onKeyDown={(e) => e.key === 'Enter' && handleCreateKB()}
                  autoFocus
                />
              </div>

              <div className="flex gap-3">
                <motion.button
                  onClick={handleCreateKB}
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                  className="flex-1 px-6 py-3 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl hover:shadow-[0_0_20px_rgba(56,189,248,0.5)] transition-all relative overflow-hidden group"
                >
                  <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
                  <span className="relative z-10">创建</span>
                </motion.button>
                <motion.button
                  onClick={() => {
                    setShowCreateDialog(false);
                    setNewKbName('');
                  }}
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                  className="flex-1 px-6 py-3 border-2 border-[#38BDF8] text-[#38BDF8] rounded-xl hover:bg-[rgba(56,189,248,0.1)] transition-all"
                >
                  取消
                </motion.button>
              </div>
            </div>
          </motion.div>
        </div>
      )}

      {/* Delete Confirmation Dialog */}
      {!isReadOnly && (
        <ConfirmDialog
          isOpen={deleteConfirm.show}
          onClose={() => setDeleteConfirm({ show: false, id: '', name: '' })}
          onConfirm={handleDeleteKB}
          title="确认删除知识库"
          message={`确定要删除知识库 "${deleteConfirm.name}" 吗？\n此操作不可恢复！`}
          type="warning"
          confirmText="删除"
          cancelText="取消"
        />
      )}

      {/* Toast Notification */}
      <Toast
        isOpen={toast.show}
        onClose={() => setToast({ ...toast, show: false })}
        message={toast.message}
        type={toast.type}
        duration={3000}
      />
    </div>
  );
}
