import { ArrowLeft, ChevronLeft, ChevronRight, Copy, Loader2 } from 'lucide-react';
import { useState, useEffect, useCallback } from 'react';
import { motion } from 'motion/react';
import { toast } from 'sonner';
import ReactMarkdown from 'react-markdown';
import { config } from '../src/config';
import { markdownRemarkPlugins, markdownRehypePlugins, normalizeMathDelimiters } from './markdownPlugins';
import { markdownTableComponents } from './markdownTableComponents';
import { ChunkCopyDialog } from './ChunkCopyDialog';
import { authFetch, appendAuthTokenToUrl } from '../src/api/auth';

interface DocumentViewerProps {
  fileId: string;
  collectionId: string; // KB collection_id, required for re-embed (v2)
  onBack: () => void;
  readOnly?: boolean;
}

interface DocumentData {
  file_id: string;
  filename: string;
  api_version?: string;
  metadata: {
    total_pages: number;
    total_images: number;
  };
  extraction_time: string;
  markdown: string;
  chunks: ChunkData[];
  pdf_url: string | null;
  total_chunks: number;
  total_pages: number;
  total_images: number;
  is_virtual_document?: boolean;
}

interface ChunkData {
  text: string;
  page_start: number;
  page_end: number;
  pages: number[];
  text_length: number;
  continued: boolean;
  cross_page_bridge: boolean;
  is_table_like: boolean;
}

interface KbChunkData {
  id: string;  // String to avoid JS number precision loss (Milvus INT64 > MAX_SAFE_INTEGER)
  chunk_text: string;
  filename: string;
  file_id: string;
  metadata: Record<string, unknown>;
  created_at?: string;
}

export function DocumentViewer({ fileId, collectionId, onBack, readOnly }: DocumentViewerProps) {
  const [activeTab, setActiveTab] = useState('original');
  const [currentPage, setCurrentPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [docData, setDocData] = useState<DocumentData | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [editedMarkdown, setEditedMarkdown] = useState('');
  const [saving, setSaving] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [kbChunksLoading, setKbChunksLoading] = useState(false);
  const [kbChunks, setKbChunks] = useState<KbChunkData[]>([]);
  const [selectedChunkIds, setSelectedChunkIds] = useState<string[]>([]);
  const [isCopyDialogOpen, setIsCopyDialogOpen] = useState(false);

  const fetchDocumentData = useCallback(async () => {
    setLoading(true);
    try {
      const url = `${config.milvusApiUrl}/document/${encodeURIComponent(fileId)}/details?collection_id=${encodeURIComponent(collectionId)}`;
      const response = await authFetch(url);
      const result = await response.json();

      if (result.status === 'success') {
        setDocData(result);
        setEditedMarkdown(result.markdown || '');
      } else {
        toast.error('获取文档详情失败');
      }
    } catch (error: unknown) {
      console.error('获取文档详情失败:', error);
      toast.error('获取文档详情失败');
    } finally {
      setLoading(false);
    }
  }, [fileId, collectionId]);

  useEffect(() => {
    fetchDocumentData();
  }, [fetchDocumentData]);

  const fetchKbChunks = useCallback(async () => {
    if (!collectionId) return;
    setKbChunksLoading(true);
    try {
      const url = `${config.milvusApiUrl}/knowledge_base/${encodeURIComponent(collectionId)}/chunks?file_id=${encodeURIComponent(fileId)}`;
      const resp = await authFetch(url);
      const res = await resp.json();
      if (!resp.ok || res?.status !== 'success') {
        throw new Error(res?.detail || res?.message || '获取切分块失败');
      }
      setKbChunks((res?.chunks || []) as KbChunkData[]);
      setSelectedChunkIds([]);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : '网络错误';
      toast.error(message);
      setKbChunks([]);
      setSelectedChunkIds([]);
    } finally {
      setKbChunksLoading(false);
    }
  }, [collectionId, fileId]);

  useEffect(() => {
    if (activeTab !== 'chunks') return;
    if (docData?.api_version !== 'v2') return;
    void fetchKbChunks();
  }, [activeTab, docData?.api_version, fetchKbChunks]);

  useEffect(() => {
    setSelectedChunkIds([]);
    setKbChunks([]);
  }, [fileId]);

  const handleEditToggle = () => {
    if (!docData) return;
    setIsEditing(!isEditing);
    setEditedMarkdown(docData.markdown || '');
  };

  const handleSaveMarkdown = async () => {
    if (!docData) return;
    setSaving(true);
    try {
      const resp = await authFetch(`${config.milvusApiUrl}/document/${fileId}/markdown`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editedMarkdown }),
      });
      const res = await resp.json();
      if (resp.ok && res?.status === 'success') {
        toast.success('Markdown 已保存');
        // Refresh to ensure viewer shows the latest
        await fetchDocumentData();
        setIsEditing(false);
      } else {
        toast.error(res?.detail || res?.message || '保存失败');
      }
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : '网络错误';
      toast.error(`保存失败: ${message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleResplitReembed = async () => {
    if (!docData) return;
    setRebuilding(true);
    try {
      const resp = await authFetch(`${config.milvusApiUrl}/document/${fileId}/resplit_reembed`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ collection_id: collectionId }),
      });
      const res = await resp.json();
      if (resp.ok && res?.status === 'success') {
        toast.success('已重切分并重建向量');
        await fetchDocumentData();
      } else {
        toast.error(res?.detail || res?.message || '操作失败');
      }
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : '网络错误';
      toast.error(`操作失败: ${message}`);
    } finally {
      setRebuilding(false);
    }
  };

  const processMarkdownImages = (markdown: string, fileId: string) => {
    // Replace relative image paths with API URLs
    // ![](images/xxx.png) -> ![](${config.milvusApiUrl}/document/{fileId}/images/xxx.png)
    const processed = markdown.replace(
      /!\[([^\]]*)\]\(images\/([^)]+)\)/g,
      `![$1](${config.milvusApiUrl}/document/${fileId}/images/$2)`
    );
    console.log('[DocumentViewer] Processing markdown images:');
    console.log('Original length:', markdown.length);
    console.log('Processed length:', processed.length);
    console.log('Sample processed:', processed.substring(0, 300));
    return processed;
  };

  const handleCopyMarkdown = () => {
    if (docData?.markdown) {
      navigator.clipboard.writeText(docData.markdown);
      toast.success('Markdown已复制到剪贴板');
    }
  };

  const tabs = [
    { id: 'original', label: '原始PDF' },
    { id: 'markdown', label: 'Markdown' },
    { id: 'chunks', label: '切分块' },
    { id: 'extraction', label: '提取信息' },
  ];

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[60vh]">
        <Loader2 size={48} className="text-[#38BDF8] animate-spin" />
      </div>
    );
  }

  if (!docData) {
    return (
      <div className="text-center py-12">
        <p className="text-[#94A3B8]">文档不存在</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <motion.div
        className="flex items-center gap-4"
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <motion.button
          onClick={onBack}
          className="text-[#38BDF8] hover:text-[#34D399] flex items-center gap-2 transition-colors group"
          whileHover={{ x: -4 }}
        >
          <ArrowLeft size={18} className="group-hover:animate-pulse" />
          返回
        </motion.button>
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center text-xl shadow-lg">
            📄
          </div>
          <h2 className="text-gradient">{docData.filename}</h2>
        </div>
      </motion.div>

      {/* Tab Navigation */}
      <motion.div
        className="glass gradient-border rounded-2xl overflow-hidden"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
      >
        <div className="border-b border-[rgba(56,189,248,0.15)]">
          <div className="flex">
            {tabs.map((tab, index) => (
              <motion.button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.2 + index * 0.05 }}
                className={`flex-1 px-6 py-4 transition-all relative ${
                  activeTab === tab.id
                    ? 'text-[#38BDF8]'
                    : 'text-[#94A3B8] hover:text-[#E2E8F0]'
                }`}
              >
                {tab.label}
                {activeTab === tab.id && (
                  <motion.div
                    layoutId="activeDocTab"
                    className="absolute bottom-0 left-0 right-0 h-0.5 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9]"
                    transition={{ type: "spring", bounce: 0.2, duration: 0.6 }}
                  />
                )}
              </motion.button>
            ))}
          </div>
        </div>

        {/* Tab Content */}
        <div className="p-6">
          {/* Original PDF Tab */}
          {activeTab === 'original' && (
            <motion.div
              className="space-y-4"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
            >
              <h3 className="text-[#E2E8F0]">PDF文档</h3>
              {docData.pdf_url ? (
                <div className="aspect-[8.5/11] glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl overflow-hidden">
                  <iframe
                    src={appendAuthTokenToUrl(`${config.milvusApiUrl}${docData.pdf_url}`)}
                    className="w-full h-full"
                    title={docData.filename}
                  />
                </div>
              ) : (
                <div className="p-6 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl text-[#94A3B8]">
                  该文档没有可用的 PDF（可能是复制出来的虚拟文档）。
                </div>
              )}
            </motion.div>
          )}

          {/* Markdown Tab */}
          {activeTab === 'markdown' && (
            <motion.div
              className="space-y-4"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
            >
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-[#E2E8F0]">Markdown 内容</h3>
                <div className="flex items-center gap-2">
                  <motion.button
                    onClick={handleCopyMarkdown}
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    className="px-3 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all flex items-center gap-2 text-[#E2E8F0]"
                  >
                    <Copy size={16} className="text-[#38BDF8]" />
                    复制
                  </motion.button>
                  {docData?.api_version === 'v2' && (
                    <>
                      {!isEditing ? (
                        <motion.button
                          onClick={handleEditToggle}
                          whileHover={{ scale: 1.05 }}
                          whileTap={{ scale: 0.95 }}
                          className="px-3 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all text-[#E2E8F0]"
                        >
                          编辑 Markdown
                        </motion.button>
                      ) : (
                        <motion.button
                          onClick={handleSaveMarkdown}
                          disabled={saving}
                          whileHover={{ scale: saving ? 1 : 1.05 }}
                          whileTap={{ scale: saving ? 1 : 0.95 }}
                          className={`px-3 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl transition-all text-[#E2E8F0] ${saving ? 'opacity-60 cursor-not-allowed' : 'hover:bg-[rgba(56,189,248,0.05)]'}`}
                        >
                          {saving ? '保存中…' : '确认保存'}
                        </motion.button>
                      )}
                      <motion.button
                        onClick={handleResplitReembed}
                        disabled={rebuilding}
                        whileHover={{ scale: rebuilding ? 1 : 1.05 }}
                        whileTap={{ scale: rebuilding ? 1 : 0.95 }}
                        className={`px-3 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl transition-all ${rebuilding ? 'opacity-60 cursor-not-allowed' : 'hover:bg-[rgba(56,189,248,0.05)]'} text-[#E2E8F0]`}
                        title="基于当前 Markdown 重新切分并重建向量"
                      >
                        {rebuilding ? '重建中…' : '重切分并重建向量'}
                      </motion.button>
                    </>
                  )}
                </div>
              </div>

              {!isEditing ? (
                <div className="p-6 rounded-xl glass-strong border border-[rgba(56,189,248,0.3)] max-h-[600px] overflow-y-auto">
                  <div className="prose prose-invert max-w-none text-[#E2E8F0]">
                    <ReactMarkdown
                      remarkPlugins={markdownRemarkPlugins}
                      rehypePlugins={markdownRehypePlugins as any}
                      components={{
                        ...markdownTableComponents,
                        h1: ({ node, ...props }) => {
                          void node;
                          return <h1 className="text-2xl text-gradient mb-4" {...props} />;
                        },
                        h2: ({ node, ...props }) => {
                          void node;
                          return <h2 className="text-xl text-[#38BDF8] mb-3" {...props} />;
                        },
                        h3: ({ node, ...props }) => {
                          void node;
                          return <h3 className="text-lg text-[#38BDF8] mb-2" {...props} />;
                        },
                        p: ({ node, ...props }) => {
                          void node;
                          return <p className="text-[#94A3B8] mb-3" {...props} />;
                        },
                        code: ({ node, ...props }) => {
                          void node;
                          return (
                            <code className="bg-[rgba(56,189,248,0.1)] text-[#34D399] px-2 py-1 rounded" {...props} />
                          );
                        },
                        pre: ({ node, ...props }) => {
                          void node;
                          return (
                            <pre className="bg-[rgba(56,189,248,0.1)] p-4 rounded-xl overflow-x-auto" {...props} />
                          );
                        },
                        img: ({ node, ...props }) => {
                          void node;
                          const src = props.src ? appendAuthTokenToUrl(props.src) : props.src;
                          return (
                            <img
                              className="max-w-full h-auto rounded-xl border border-[rgba(56,189,248,0.2)] my-4"
                              {...props}
                              src={src}
                            />
                          );
                        },
                      }}
                    >
                      {normalizeMathDelimiters(
                        processMarkdownImages(docData.markdown, docData.file_id)
                      )}
                    </ReactMarkdown>
                  </div>
                </div>
              ) : (
                <div className="p-4 rounded-xl glass-strong border border-[rgba(56,189,248,0.3)]">
                  <textarea
                    value={editedMarkdown}
                    onChange={(e) => setEditedMarkdown(e.target.value)}
                    className="w-full h-[480px] p-3 bg-transparent border border-[rgba(56,189,248,0.2)] rounded-xl text-[#E2E8F0] font-mono text-sm outline-none focus:ring-2 focus:ring-[#38BDF8]"
                  />
                </div>
              )}
            </motion.div>
          )}

          {/* Chunks Tab */}
          {activeTab === 'chunks' && (
            <motion.div
              className="space-y-4"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
            >
              <div className="flex items-center justify-between mb-4 gap-3">
                <h3 className="text-[#E2E8F0]">
                  共 {docData.total_chunks} 个切分块
                  {kbChunksLoading && <span className="text-[#94A3B8] text-sm ml-2">加载中…</span>}
                </h3>

                {docData.api_version === 'v2' && !readOnly && (
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-2 text-[#94A3B8] select-none">
                      <input
                        type="checkbox"
                        checked={kbChunks.length > 0 && selectedChunkIds.length === kbChunks.length}
                        onChange={() => {
                          if (kbChunks.length === 0) return;
                          if (selectedChunkIds.length === kbChunks.length) {
                            setSelectedChunkIds([]);
                          } else {
                            setSelectedChunkIds(kbChunks.map((c) => c.id));
                          }
                        }}
                      />
                      全选
                    </label>
                    <span className="text-[#94A3B8] text-sm">
                      已选: <span className="text-[#38BDF8]">{selectedChunkIds.length}</span>/{kbChunks.length || docData.total_chunks}
                    </span>
                    <motion.button
                      onClick={() => setIsCopyDialogOpen(true)}
                      disabled={selectedChunkIds.length === 0}
                      whileHover={{ scale: selectedChunkIds.length === 0 ? 1 : 1.05 }}
                      whileTap={{ scale: selectedChunkIds.length === 0 ? 1 : 0.95 }}
                      className={`px-3 py-2 rounded-xl transition-all flex items-center gap-2 ${
                        selectedChunkIds.length === 0
                          ? 'opacity-60 cursor-not-allowed glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0]'
                          : 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A]'
                      }`}
                      title="复制到其他知识库（直接复制向量）"
                    >
                      <Copy size={16} />
                      复制到其他知识库…
                    </motion.button>
                  </div>
                )}
              </div>

              <div className="space-y-4 max-h-[600px] overflow-y-auto">
                {(docData.api_version === 'v2' && kbChunks.length > 0
                  ? kbChunks.map((ch) => {
                      const md = (ch.metadata || {}) as Record<string, unknown>;
                      const pageStart = Number(md['page_start'] ?? 1);
                      const pageEnd = Number(md['page_end'] ?? pageStart);
                      const textLength = Number(md['text_length'] ?? (ch.chunk_text?.length ?? 0));
                      const continued = Boolean(md['continued']);
                      const crossPageBridge = Boolean(md['cross_page_bridge']);
                      const isTableLike = Boolean(md['is_table_like']);
                      const chunkIndex = Number(md['chunk_index'] ?? 0);
                      return {
                        key: ch.id,
                        id: ch.id,
                        index: chunkIndex,
                        text: ch.chunk_text,
                        page_start: pageStart,
                        page_end: pageEnd,
                        text_length: textLength,
                        continued,
                        cross_page_bridge: crossPageBridge,
                        is_table_like: isTableLike,
                      };
                    })
                  : docData.chunks.map((chunk, index) => ({
                      key: index,
                      id: String(index),
                      index,
                      text: chunk.text,
                      page_start: chunk.page_start,
                      page_end: chunk.page_end,
                      text_length: chunk.text_length,
                      continued: chunk.continued,
                      cross_page_bridge: chunk.cross_page_bridge,
                      is_table_like: chunk.is_table_like,
                    }))
                ).map((chunk, index) => {
                  const hasMarkdownTable = /(^|\n)\|.+\|\s*\n\|[-:| ]+\|/m.test(chunk.text);
                  const shouldRenderMarkdown =
                    chunk.is_table_like || chunk.text.includes('<table') || hasMarkdownTable;

                  return (
                    <motion.div
                      key={chunk.key}
                      initial={{ opacity: 0, y: 20 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: index * 0.05 }}
                      whileHover={{ y: -2 }}
                      className="glass gradient-border rounded-xl p-5 hover:shadow-[0_0_20px_rgba(56,189,248,0.2)] transition-all group"
                    >
                      <div className="flex items-center gap-3 mb-4">
                        {docData.api_version === 'v2' && kbChunks.length > 0 && !readOnly && (
                          <input
                            type="checkbox"
                            checked={selectedChunkIds.includes(chunk.id)}
                            onChange={() => {
                              setSelectedChunkIds((prev) =>
                                prev.includes(chunk.id) ? prev.filter((x) => x !== chunk.id) : [...prev, chunk.id]
                              );
                            }}
                          />
                        )}
                        <span className="text-[#E2E8F0]">Chunk #{chunk.index + 1}</span>
                        <span className="text-[#94A3B8] flex items-center gap-1">
                          📍第 {chunk.page_start}{chunk.page_start !== chunk.page_end ? `-${chunk.page_end}` : ''} 页
                        </span>
                        <span className="text-[#94A3B8] flex items-center gap-1">
                          📏 {chunk.text_length} 字符
                        </span>
                      </div>

                      {shouldRenderMarkdown ? (
                        <div className="p-4 glass-strong rounded-xl border border-[rgba(56,189,248,0.1)] text-sm mb-4 max-h-[200px] overflow-y-auto">
                          <ReactMarkdown
                            remarkPlugins={markdownRemarkPlugins}
                            rehypePlugins={markdownRehypePlugins as any}
                            components={{
                              ...markdownTableComponents,
                              p: ({ node, ...props }) => {
                                void node;
                                return <p className="text-[#94A3B8] mb-2 leading-relaxed" {...props} />;
                              },
                              img: ({ node, ...props }) => {
                                void node;
                                const src = props.src ? appendAuthTokenToUrl(props.src) : props.src;
                                return (
                                  <img
                                    className="max-w-full h-auto rounded-xl border border-[rgba(56,189,248,0.2)] my-3"
                                    {...props}
                                    src={src}
                                  />
                                );
                              },
                            }}
                          >
                            {normalizeMathDelimiters(chunk.text)}
                          </ReactMarkdown>
                        </div>
                      ) : (
                        <div className="p-4 glass-strong rounded-xl border border-[rgba(56,189,248,0.1)] font-mono text-sm mb-4 text-[#94A3B8] max-h-[200px] overflow-y-auto whitespace-pre-wrap">
                          {chunk.text}
                        </div>
                      )}

                      <div className="flex gap-2">
                        <span className={`px-3 py-1 rounded-lg text-xs ${chunk.cross_page_bridge ? 'bg-[rgba(52,211,153,0.1)] text-[#34D399] border border-[rgba(52,211,153,0.2)]' : 'bg-[rgba(148,163,184,0.1)] text-[#94A3B8] border border-[rgba(148,163,184,0.2)]'}`}>
                          跨页: {chunk.cross_page_bridge ? '✅' : '❌'}
                        </span>
                        <span className={`px-3 py-1 rounded-lg text-xs ${chunk.continued ? 'bg-[rgba(52,211,153,0.1)] text-[#34D399] border border-[rgba(52,211,153,0.2)]' : 'bg-[rgba(148,163,184,0.1)] text-[#94A3B8] border border-[rgba(148,163,184,0.2)]'}`}>
                          续接: {chunk.continued ? '✅' : '❌'}
                        </span>
                        <span className={`px-3 py-1 rounded-lg text-xs ${chunk.is_table_like ? 'bg-[rgba(52,211,153,0.1)] text-[#34D399] border border-[rgba(52,211,153,0.2)]' : 'bg-[rgba(148,163,184,0.1)] text-[#94A3B8] border border-[rgba(148,163,184,0.2)]'}`}>
                          表格: {chunk.is_table_like ? '✅' : '❌'}
                        </span>
                      </div>
                    </motion.div>
                  );
                })}
              </div>
            </motion.div>
          )}

          <ChunkCopyDialog
            isOpen={isCopyDialogOpen}
            onClose={() => setIsCopyDialogOpen(false)}
            sourceCollectionId={collectionId}
            sourceFilename={docData.filename}
            sourceFileId={fileId}
            selectedChunkIds={selectedChunkIds}
            onCopied={() => {
              setSelectedChunkIds([]);
              void fetchKbChunks();
            }}
          />

          {/* Extraction Info Tab */}
          {activeTab === 'extraction' && (
            <motion.div
              className="grid grid-cols-3 gap-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
            >
              <div className="space-y-4">
                <h3 className="text-[#E2E8F0] flex items-center gap-2">
                  <span className="text-2xl">📄</span>
                  文档信息
                </h3>
                <div className="space-y-3">
                  <div className="p-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl">
                    <p className="text-[#94A3B8] mb-2">总页数</p>
                    <div className="text-2xl text-[#38BDF8]">{docData.total_pages}</div>
                  </div>
                  <div className="p-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl">
                    <p className="text-[#94A3B8] mb-2">提取时间</p>
                    <div className="text-sm text-[#38BDF8]">
                      {new Date(docData.extraction_time).toLocaleString('zh-CN')}
                    </div>
                  </div>
                </div>
              </div>

              <div className="space-y-4">
                <h3 className="text-[#E2E8F0] flex items-center gap-2">
                  <span className="text-2xl">🔢</span>
                  切分统计
                </h3>
                <div className="space-y-3">
                  <div className="p-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl">
                    <p className="text-[#94A3B8] mb-2">总切分块数</p>
                    <div className="text-2xl text-[#38BDF8]">{docData.total_chunks}</div>
                  </div>
                  <div className="p-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl">
                    <p className="text-[#94A3B8] mb-2">跨页块数</p>
                    <div className="text-2xl text-[#34D399]">
                      {docData.chunks.filter(c => c.cross_page_bridge).length}
                    </div>
                  </div>
                </div>
              </div>

              <div className="space-y-4">
                <h3 className="text-[#E2E8F0] flex items-center gap-2">
                  <span className="text-2xl">🖼️</span>
                  图片统计
                </h3>
                <div className="space-y-3">
                  <div className="p-4 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl">
                    <p className="text-[#94A3B8] mb-2">提取图片数</p>
                    <div className="text-2xl text-[#38BDF8]">{docData.total_images}</div>
                  </div>
                </div>
              </div>
            </motion.div>
          )}

          {/* Page Navigation - Only for PDF tab */}
          {activeTab === 'original' && docData.total_pages > 1 && (
            <motion.div
              className="flex items-center justify-center gap-4 mt-6 pt-6 border-t border-[rgba(56,189,248,0.15)]"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.3 }}
            >
              <motion.button
                onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                disabled={currentPage === 1}
                whileHover={{ scale: currentPage === 1 ? 1 : 1.05 }}
                whileTap={{ scale: currentPage === 1 ? 1 : 0.95 }}
                className="px-4 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 text-[#E2E8F0]"
              >
                <ChevronLeft size={16} />
                上一页
              </motion.button>

              <span className="text-[#94A3B8] px-4 py-2 glass rounded-xl border border-[rgba(56,189,248,0.2)]">
                第 <span className="text-[#38BDF8]">{currentPage}</span> / {docData.total_pages} 页
              </span>

              <motion.button
                onClick={() => setCurrentPage(Math.min(docData.total_pages, currentPage + 1))}
                disabled={currentPage === docData.total_pages}
                whileHover={{ scale: currentPage === docData.total_pages ? 1 : 1.05 }}
                whileTap={{ scale: currentPage === docData.total_pages ? 1 : 0.95 }}
                className="px-4 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 text-[#E2E8F0]"
              >
                下一页
                <ChevronRight size={16} />
              </motion.button>
            </motion.div>
          )}
        </div>
      </motion.div>
    </div>
  );
}
