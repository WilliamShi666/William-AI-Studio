import { Loader2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { toast } from 'sonner';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';

interface KnowledgeBaseOption {
  collection_id: string;
  display_name: string;
  created_at?: string;
}

interface ChunkCopyDialogProps {
  isOpen: boolean;
  onClose: () => void;
  sourceCollectionId: string;
  sourceFilename: string;
  sourceFileId: string;
  selectedChunkIds: string[];  // String IDs to avoid JS number precision loss
  onCopied?: (targetCollectionId: string) => void;
}

export function ChunkCopyDialog({
  isOpen,
  onClose,
  sourceCollectionId,
  sourceFilename,
  sourceFileId,
  selectedChunkIds,
  onCopied,
}: ChunkCopyDialogProps) {
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseOption[]>([]);
  const [targetCollectionId, setTargetCollectionId] = useState<string>('');

  const selectableKnowledgeBases = useMemo(() => {
    return knowledgeBases
      .filter((kb) => kb.collection_id !== sourceCollectionId)
      .filter((kb) => kb.display_name?.endsWith('_v2'));
  }, [knowledgeBases, sourceCollectionId]);

  const fetchKnowledgeBases = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await authFetch(`${config.milvusApiUrl}/knowledge_base/list`);
      const res = await resp.json();
      if (!resp.ok) {
        throw new Error(res?.detail || res?.message || '获取知识库列表失败');
      }
      const kbs = (res?.knowledge_bases || []) as Array<{
        collection_id: string;
        display_name: string;
        created_at?: string;
      }>;
      setKnowledgeBases(
        kbs.map((kb) => ({
          collection_id: kb.collection_id,
          display_name: kb.display_name || kb.collection_id,
          created_at: kb.created_at,
        }))
      );
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : '网络错误';
      toast.error(message);
      setKnowledgeBases([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    void fetchKnowledgeBases();
    setTargetCollectionId('');
  }, [isOpen, fetchKnowledgeBases]);

  const handleSubmit = async () => {
    if (!targetCollectionId) {
      toast.error('请选择目标知识库');
      return;
    }
    if (selectedChunkIds.length === 0) {
      toast.error('请选择要复制的切分块');
      return;
    }

    setSubmitting(true);
    try {
      const newFilename = `【复制】${sourceFilename}（${selectedChunkIds.length} chunks）`;
      const resp = await authFetch(`${config.milvusApiUrl}/chunks/copy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_collection_id: sourceCollectionId,
          target_collection_id: targetCollectionId,
          chunk_ids: selectedChunkIds,
          new_filename: newFilename,
          new_file_id: `copied_${sourceFileId}_${Date.now()}`,
        }),
      });
      const res = await resp.json();
      if (!resp.ok || res?.status !== 'success') {
        throw new Error(res?.detail || res?.message || '复制失败');
      }
      const copiedCount = Number(res?.copied_count ?? 0);
      const requestedCount = Number(res?.requested_count ?? selectedChunkIds.length);
      if (copiedCount === requestedCount) {
        toast.success(`已复制 ${copiedCount} 个切分块`);
      } else {
        toast.warning(`复制完成：${copiedCount}/${requestedCount}`, {
          description: '部分切分块未复制成功，请查看 backend/logs/chunk_copy.log 详情',
        });
      }
      onClose();
      onCopied?.(targetCollectionId);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : '网络错误';
      toast.error(`复制失败: ${message}`);
    } finally {
      setSubmitting(false);
    }
  };

  if (!isOpen) return null;

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
      >
        <motion.div
          className="w-full max-w-[560px] glass gradient-border rounded-2xl p-6"
          initial={{ opacity: 0, y: 20, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 10, scale: 0.98 }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-[#E2E8F0]">复制切分块到其他知识库</h3>
            <button
              onClick={onClose}
              className="text-[#94A3B8] hover:text-[#E2E8F0] transition-colors"
              aria-label="关闭"
            >
              <X size={18} />
            </button>
          </div>

          <div className="space-y-4">
            <div className="p-4 glass-strong border border-[rgba(56,189,248,0.15)] rounded-xl">
              <div className="text-[#94A3B8] text-sm">已选择</div>
              <div className="text-[#E2E8F0] text-lg">{selectedChunkIds.length} 个切分块</div>
              <div className="text-[#94A3B8] text-sm mt-2">
                来源：{sourceFilename}（{sourceCollectionId}）
              </div>
            </div>

            <div className="space-y-2">
              <div className="text-[#94A3B8] text-sm">目标知识库（仅 v2）</div>
              {loading ? (
                <div className="flex items-center gap-2 text-[#94A3B8]">
                  <Loader2 size={16} className="animate-spin" />
                  加载中…
                </div>
              ) : (
                <select
                  value={targetCollectionId}
                  onChange={(e) => setTargetCollectionId(e.target.value)}
                  className="w-full px-3 py-2 rounded-xl bg-transparent border border-[rgba(56,189,248,0.25)] text-[#E2E8F0] outline-none focus:ring-2 focus:ring-[#38BDF8]"
                >
                  <option value="" className="bg-[#0F172A]">
                    请选择目标知识库
                  </option>
                  {selectableKnowledgeBases.map((kb) => (
                    <option key={kb.collection_id} value={kb.collection_id} className="bg-[#0F172A]">
                      {kb.display_name}
                    </option>
                  ))}
                </select>
              )}
            </div>

            <div className="text-[#94A3B8] text-sm">
              切分块将直接复制（包含向量），无需重新进行 Embedding 计算。
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <motion.button
                onClick={onClose}
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                className="px-4 py-2 glass-strong border border-[rgba(148,163,184,0.2)] rounded-xl text-[#E2E8F0] hover:bg-[rgba(148,163,184,0.05)] transition-all"
              >
                取消
              </motion.button>
              <motion.button
                onClick={handleSubmit}
                disabled={submitting || loading}
                whileHover={{ scale: submitting || loading ? 1 : 1.02 }}
                whileTap={{ scale: submitting || loading ? 1 : 0.98 }}
                className={`px-4 py-2 rounded-xl transition-all flex items-center gap-2 ${
                  submitting || loading
                    ? 'opacity-60 cursor-not-allowed glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0]'
                    : 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A]'
                }`}
              >
                {submitting && <Loader2 size={16} className="animate-spin" />}
                确认复制
              </motion.button>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
