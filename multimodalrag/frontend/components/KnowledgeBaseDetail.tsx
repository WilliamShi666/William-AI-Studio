import { ArrowLeft, Search, Upload, Eye, Trash2, ChevronDown, Loader2 } from 'lucide-react';
import { useState, useEffect, useCallback } from 'react';
import { motion } from 'motion/react';
import { toast } from 'sonner';
import { UploadDialog, type UploadConfig } from './UploadDialog';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';
import { MODE_CONFIG, ModeId } from '../src/modes';

interface KnowledgeBaseDetailProps {
  collectionId: string;
  onBack: () => void;
  onViewDocument: (fileId: string) => void;
  mode: ModeId;
}

interface DocumentData {
  filename: string;
  file_id: string;
  chunks: number;
  created_at: string;
  metadata?: Record<string, unknown>;
}

interface KBInfo {
  collection_id: string;
  collection_name: string;
  total_documents: number;
  total_chunks: number;
  last_updated: string | null;
}

export function KnowledgeBaseDetail({ collectionId, onBack, onViewDocument, mode }: KnowledgeBaseDetailProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [documents, setDocuments] = useState<DocumentData[]>([]);
  const [kbInfo, setKbInfo] = useState<KBInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [showUploadDialog, setShowUploadDialog] = useState(false);
  const modeConfig = MODE_CONFIG[mode];
  const isReadOnly = modeConfig.readOnly;

  const fetchKBDetails = useCallback(async () => {
    setLoading(true);
    try {
      const response = await authFetch(`${config.milvusApiUrl}/knowledge_base/${collectionId}/documents`);
      const result = await response.json();

      if (result.status === 'success') {
        setKbInfo({
          collection_id: result.collection_id,
          collection_name: result.collection_name,
          total_documents: result.total_documents,
          total_chunks: result.total_chunks,
          last_updated: result.last_updated,
        });
        setDocuments(result.documents || []);
      } else {
        toast.error('获取知识库详情失败');
      }
    } catch (error: unknown) {
      console.error('获取知识库详情失败:', error);
      const message = error instanceof Error ? error.message : '未知错误';
      toast.error(`获取知识库详情失败: ${message}`);
    } finally {
      setLoading(false);
    }
  }, [collectionId]);

  useEffect(() => {
    fetchKBDetails();
  }, [fetchKBDetails]);

  const getFileIcon = (filename: string) => {
    const ext = filename.split('.').pop()?.toLowerCase();
    switch (ext) {
      case 'pdf':
        return '📄';
      case 'md':
      case 'txt':
        return '📝';
      case 'docx':
      case 'doc':
        return '📘';
      case 'jpg':
      case 'jpeg':
      case 'png':
      case 'gif':
        return '🖼️';
      default:
        return '📄';
    }
  };

  const formatDate = (dateStr: string) => {
    if (!dateStr) return '未知';
    try {
      const date = new Date(dateStr);
      return date.toLocaleDateString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return dateStr;
    }
  };

  const getRelativeTime = (dateStr: string | null) => {
    if (!dateStr) return '未知';
    try {
      const date = new Date(dateStr);
      const now = new Date();
      const diff = now.getTime() - date.getTime();
      const minutes = Math.floor(diff / 60000);
      const hours = Math.floor(minutes / 60);
      const days = Math.floor(hours / 24);

      if (minutes < 60) return `${minutes}分钟前`;
      if (hours < 24) return `${hours}小时前`;
      if (days < 7) return `${days}天前`;
      return formatDate(dateStr);
    } catch {
      return dateStr;
    }
  };

  const filteredDocuments = documents.filter((doc) =>
    doc.filename.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const handleUpload = (files: File[], kbId: string, uploadConfig: UploadConfig) => {
    console.log('Uploading files:', files, 'to KB:', kbId, 'with config:', uploadConfig);
    // 上传完成后刷新文档列表
    fetchKBDetails();
  };

  // 删除单个文档（按 file_id）
  const handleDeleteDocument = async (fileId: string, filename: string) => {
    if (isReadOnly) {
      toast.error('当前模式不支持删除文档');
      return;
    }

    const ok = window.confirm(`确定要删除文档 “${filename}” 吗？\n此操作会删除其所有向量并且不可恢复。`);
    if (!ok) return;

    try {
      const baseUrl = config.milvusApiUrl;
      const url = `${baseUrl}/document/${encodeURIComponent(fileId)}?collection_id=${encodeURIComponent(collectionId)}`;
      const resp = await authFetch(url, { method: 'DELETE' });
      const result = await resp.json();

      if (resp.ok && result?.status === 'success') {
        toast.success(`已删除：${filename}`);
        // 刷新文档列表与统计
        fetchKBDetails();
      } else {
        const msg = result?.detail || result?.message || '删除失败';
        toast.error(msg);
      }
    } catch (error: unknown) {
      console.error('删除文档失败:', error);
      const message = error instanceof Error ? error.message : '未知错误';
      toast.error(`删除文档失败: ${message}`);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[60vh]">
        <Loader2 size={48} className="text-[#38BDF8] animate-spin" />
      </div>
    );
  }

  if (!kbInfo) {
    return (
      <div className="text-center py-12">
        <p className="text-[#94A3B8]">知识库不存在</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <motion.div
        className="flex items-center justify-between"
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="flex items-center gap-4">
          <motion.button
            onClick={onBack}
            className="text-[#38BDF8] hover:text-[#34D399] flex items-center gap-2 transition-colors group"
            whileHover={{ x: -4 }}
          >
            <ArrowLeft size={18} className="group-hover:animate-pulse" />
            返回
          </motion.button>
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center text-2xl shadow-lg">
              📘
            </div>
            <h2 className="text-gradient">{kbInfo.collection_name}</h2>
          </div>
        </div>

        {!isReadOnly && (
          <motion.button
            onClick={() => setShowUploadDialog(true)}
            className="px-6 py-3 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-xl hover:shadow-[0_0_30px_rgba(56,189,248,0.6)] transition-all flex items-center gap-2 relative overflow-hidden group"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
            <Upload size={18} className="relative z-10" />
            <span className="relative z-10">上传文档</span>
          </motion.button>
        )}
      </motion.div>

      {/* Upload Dialog */}
      {!isReadOnly && (
        <UploadDialog
          isOpen={showUploadDialog}
          onClose={() => setShowUploadDialog(false)}
          onUpload={handleUpload}
          preselectedKB={collectionId}
          mode={mode}
        />
      )}

      {/* Summary Bar */}
      <motion.div
        className="flex items-center gap-3"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
      >
        <span className="px-4 py-2 glass-strong rounded-xl border border-[rgba(56,189,248,0.2)] text-[#E2E8F0]">
          {kbInfo.total_documents}个文档
        </span>
        <span className="px-4 py-2 glass-strong rounded-xl border border-[rgba(52,211,153,0.2)] text-[#34D399]">
          {kbInfo.total_chunks} chunks
        </span>
        <span className="px-4 py-2 glass-strong rounded-xl border border-[rgba(56,189,248,0.2)] text-[#94A3B8]">
          最后更新: {getRelativeTime(kbInfo.last_updated)}
        </span>
      </motion.div>

      {/* Search and Filters */}
      <motion.div
        className="flex items-center gap-4"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
      >
        <div className="relative flex-1 max-w-[320px]">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-[#38BDF8]" size={18} />
          <input
            type="text"
            placeholder="搜索文档..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full h-12 pl-11 pr-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl focus:outline-none focus:ring-2 focus:ring-[#38BDF8] focus:border-transparent text-[#E2E8F0] placeholder-[#94A3B8] transition-all"
          />
        </div>

        <motion.button
          className="px-4 py-3 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all flex items-center gap-2 text-[#E2E8F0]"
          whileHover={{ scale: 1.05 }}
        >
          按时间
          <ChevronDown size={16} className="text-[#38BDF8]" />
        </motion.button>

        <motion.button
          className="px-4 py-3 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all flex items-center gap-2 text-[#E2E8F0]"
          whileHover={{ scale: 1.05 }}
        >
          按大小
          <ChevronDown size={16} className="text-[#38BDF8]" />
        </motion.button>
      </motion.div>

      {/* Document List */}
      {filteredDocuments.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center py-12"
        >
          <p className="text-[#94A3B8]">
            {documents.length === 0 ? '暂无文档' : '没有找到匹配的文档'}
          </p>
        </motion.div>
      ) : (
        <div className="space-y-3">
          {filteredDocuments.map((doc, index) => (
            <motion.div
              key={doc.file_id}
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4, delay: 0.3 + index * 0.1 }}
              whileHover={{ y: -2 }}
              className="glass gradient-border rounded-xl p-5 hover:shadow-[0_0_25px_rgba(56,189,248,0.2)] transition-all group relative overflow-hidden"
            >
              {/* Hover shimmer */}
              <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500">
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-[rgba(56,189,248,0.1)] to-transparent shimmer" />
              </div>

              <div className="flex items-center gap-4 relative z-10">
                {/* File Icon */}
                <div className="w-14 h-14 glass-strong rounded-xl flex items-center justify-center text-3xl flex-shrink-0 border border-[rgba(56,189,248,0.2)] group-hover:scale-110 transition-transform">
                  {getFileIcon(doc.filename)}
                </div>

                {/* File Info */}
                <div className="flex-1 min-w-0">
                  <div className="text-[#E2E8F0] mb-2 group-hover:text-[#38BDF8] transition-colors truncate" title={doc.filename}>
                    {doc.filename}
                  </div>
                  <div className="text-[#94A3B8] text-sm flex items-center gap-3">
                    <span className="px-2 py-1 glass rounded-lg border border-[rgba(56,189,248,0.1)]">
                      {doc.chunks} chunks
                    </span>
                    <span>上传于 {formatDate(doc.created_at)}</span>
                  </div>
                </div>

                {/* Status */}
                <div className="flex items-center gap-2">
                  <span className="px-3 py-1.5 bg-[rgba(52,211,153,0.1)] text-[#34D399] rounded-lg text-xs border border-[rgba(52,211,153,0.2)] flex items-center gap-1">
                    ✅ 已索引
                  </span>
                </div>

                {/* Actions */}
                <div className="flex gap-2">
                  <motion.button
                    onClick={() => onViewDocument(doc.file_id)}
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    className="px-4 py-2 border border-[#38BDF8] text-[#38BDF8] rounded-xl hover:bg-[rgba(56,189,248,0.1)] transition-all flex items-center gap-2"
                  >
                    <Eye size={16} />
                    查看
                  </motion.button>
                  <motion.button
                    onClick={() => handleDeleteDocument(doc.file_id, doc.filename)}
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    className="px-4 py-2 border border-[#ff3b5c] text-[#ff3b5c] rounded-xl hover:bg-[rgba(255,59,92,0.1)] transition-all"
                    title="删除文档"
                  >
                    <Trash2 size={16} />
                  </motion.button>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}
