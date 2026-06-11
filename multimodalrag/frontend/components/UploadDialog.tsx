import { useState, useEffect, useCallback } from 'react';
import { X, Upload, FileText, Settings, AlertCircle, CheckCircle, Loader2, Plus } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { toast } from 'sonner';
import { getExtractionMethods, getUploadEndpoint, getDefaultExtractionMethod } from '../src/api/config';
import { config as appConfig } from '../src/config';
import { authFetch } from '../src/api/auth';
import { MODE_CONFIG, ModeId, isModeUsingV2Data } from '../src/modes';

interface UploadDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onUpload: (files: File[], kbId: string, config: UploadConfig) => void;
  preselectedKB?: string; // 预选的知识库ID
  mode: ModeId;
}

export interface UploadConfig {
  extractionMode: string; // 支持 v1: 'fast'|'vlm', v2: 'mineru'|'paddleocr'|'deepseek'
  chunkSize: number;
  overlap: number;
  maxPageSpan: number;
  bridgeLength: number;
  chunkingMethod: string;
}

interface KnowledgeBaseOption {
  collection_id: string;
  collection_name: string;
  total_documents: number;
  total_chunks: number;
  displayName: string;
  isV2: boolean;
}

export function UploadDialog({ isOpen, onClose, onUpload, preselectedKB, mode }: UploadDialogProps) {
  const modeConfig = MODE_CONFIG[mode];
  const usesV2Extraction = isModeUsingV2Data(mode);
  const useLightTheme = modeConfig.theme === 'light';
  const [selectedKB, setSelectedKB] = useState<string | null>(preselectedKB || null);
  const [files, setFiles] = useState<File[]>([]);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{ [key: string]: string }>({});
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [showCreateKB, setShowCreateKB] = useState(false);
  const [newKBName, setNewKBName] = useState('');
  
  // 根据模式初始化配置
  const [uploadConfig, setUploadConfig] = useState<UploadConfig>({
    extractionMode: getDefaultExtractionMethod(usesV2Extraction),
    chunkSize: 1500,
    overlap: 200,
    maxPageSpan: 3,
    bridgeLength: 100,
    chunkingMethod: usesV2Extraction ? 'ocr_aware' : 'header_recursive',
  });
  
  useEffect(() => {
    setUploadConfig((prev) => ({
      ...prev,
      extractionMode: getDefaultExtractionMethod(usesV2Extraction),
      chunkingMethod: usesV2Extraction ? 'ocr_aware' : 'header_recursive',
    }));
  }, [usesV2Extraction]);
  
  // 获取当前版本的提取方法列表
  const extractionMethods = getExtractionMethods(usesV2Extraction);

  const fetchKnowledgeBases = useCallback(async () => {
    setLoading(true);
    try {
      // 确保 API URL 不为 undefined
      const baseUrl = appConfig.milvusApiUrl || import.meta.env.VITE_MILVUS_API_URL || 'http://localhost:8000';
      const apiUrl = `${baseUrl}/stats/all`;
      console.log('[UploadDialog] 请求 URL:', apiUrl);
      console.log('[UploadDialog] config.milvusApiUrl:', appConfig.milvusApiUrl);
      console.log('[UploadDialog] import.meta.env.VITE_MILVUS_API_URL:', import.meta.env.VITE_MILVUS_API_URL);
      console.log('[UploadDialog] baseUrl:', baseUrl);

      const response = await authFetch(apiUrl);
      console.log('[UploadDialog] 响应状态:', response.status);
      console.log('[UploadDialog] Content-Type:', response.headers.get('content-type'));

      const text = await response.text();
      console.log('[UploadDialog] 响应文本前100字符:', text.substring(0, 100));

      const result = JSON.parse(text);
      console.log('[UploadDialog] 解析成功:', result);

      if (result.status === 'success') {
        const collections = result.data.collections || [];
        const processedCollections: KnowledgeBaseOption[] = collections.map((col: KnowledgeBaseOption) => {
          const isV2Collection = col.collection_name.endsWith('_v2');
          return {
            ...col,
            isV2: isV2Collection,
            displayName: isV2Collection ? col.collection_name.slice(0, -3) : col.collection_name,
          };
        });

        setKnowledgeBases(processedCollections);
      }
    } catch (error) {
      const err = error instanceof Error ? error : new Error(String(error));
      console.error('获取知识库列表失败:', err);
      console.error('[UploadDialog] 错误详情:', err.message, err.stack);
      toast.error('获取知识库列表失败: ' + err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // 从Milvus API获取知识库列表
  useEffect(() => {
    if (isOpen) {
      fetchKnowledgeBases();
    }
  }, [isOpen, fetchKnowledgeBases, mode]);

  // 当预选知识库变化时更新选中状态
  useEffect(() => {
    if (preselectedKB) {
      setSelectedKB(preselectedKB);
    }
  }, [preselectedKB]);

  const handleCreateKB = async () => {
    if (!newKBName.trim()) {
      toast.error('请输入知识库名称');
      return;
    }

    try {
      // ✅ V2模式：自动添加 _v2 后缀
      const actualKBName = usesV2Extraction ? `${newKBName}_v2` : newKBName;
      
      const baseUrl = appConfig.milvusApiUrl || import.meta.env.VITE_MILVUS_API_URL || 'http://localhost:8000';
      const response = await authFetch(
        `${baseUrl}/knowledge_base/create?display_name=${encodeURIComponent(actualKBName)}`,
        { method: 'POST' }
      );
      const result = await response.json();

      if (result.status === 'success') {
        toast.success(result.message);
        setShowCreateKB(false);
        setNewKBName('');
        // 刷新列表并自动选中新创建的知识库
        await fetchKnowledgeBases();
        setSelectedKB(result.collection_id);
      } else {
        toast.error(result.message || '创建知识库失败');
      }
    } catch (error) {
      const err = error instanceof Error ? error : new Error(String(error));
      console.error('创建知识库失败:', err);
      toast.error('创建知识库失败: ' + err.message);
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      setFiles(Array.from(e.target.files));
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    if (e.dataTransfer.files) {
      setFiles(Array.from(e.dataTransfer.files));
    }
  };

  const handleSubmit = async () => {
    if (!selectedKB || files.length === 0) return;

    setUploading(true);
    // 根据版本获取上传端点
    const uploadEndpoint = getUploadEndpoint(usesV2Extraction);
    console.log('[Upload] Starting upload:', { endpoint: uploadEndpoint, files: files.length, kb: selectedKB });

    try {
      // 逐个上传文件
      for (const file of files) {
        console.log('[Upload] Processing file:', file.name);
        setUploadProgress((prev) => ({ ...prev, [file.name]: 'uploading' }));

        const formData = new FormData();
        formData.append('file', file);
        formData.append('knowledge_base_id', selectedKB);  // 直接使用collection_id
        formData.append('auto_extract', 'true');
        formData.append('extraction_mode', uploadConfig.extractionMode);
        formData.append('auto_chunk', 'true');
        formData.append('chunking_method', uploadConfig.chunkingMethod);
        formData.append('chunk_size', uploadConfig.chunkSize.toString());
        formData.append('chunk_overlap', uploadConfig.overlap.toString());
        formData.append('max_page_span', uploadConfig.maxPageSpan.toString());

        try {
          console.log('[Upload] Sending request:', file.name);
          const response = await authFetch(uploadEndpoint, {
            method: 'POST',
            body: formData,
          });

          console.log('[Upload] Response received:', { status: response.status, file: file.name });
          const result = await response.json();
          console.log('[Upload] Response data:', result);

          if (result.success) {
            setUploadProgress((prev) => ({ ...prev, [file.name]: 'success' }));

            // 构建成功消息
            let description = '文件已保存';
            if (result.data.extraction?.status === 'completed') {
              const pages = result.data.extraction.total_pages;
              const images = result.data.extraction.total_images;
              description = `已提取 ${pages} 页`;
              if (images > 0) {
                description += `，${images} 张图片`;
              }

              // 如果有切分结果
              if (result.data.chunking?.status === 'completed') {
                const chunks = result.data.chunking.total_chunks;
                description += `，已切分为 ${chunks} 个块`;
              }
            }

            toast.success(`${file.name} 上传成功`, {
              description,
            });
          } else {
            setUploadProgress((prev) => ({ ...prev, [file.name]: 'error' }));
            toast.error(`${file.name} 上传失败`, {
              description: result.error?.message || '未知错误',
            });
          }
        } catch (error) {
          console.error('[Upload] Error:', { file: file.name, error });
          setUploadProgress((prev) => ({ ...prev, [file.name]: 'error' }));

          let errorMsg = '网络错误';
          if (error instanceof Error) {
            if (error.name === 'AbortError') {
              errorMsg = '上传已取消或连接被中断';
            } else {
              errorMsg = error.message;
            }
          }

          toast.error(`${file.name} 上传失败`, {
            description: errorMsg,
          });
        }
      }

      // 所有文件上传完成后，调用父组件的回调
      console.log('[Upload] All files processed');
      onUpload(files, selectedKB, uploadConfig);

      // 延迟关闭对话框，让用户看到结果
      setTimeout(() => {
        onClose();
        setFiles([]);
        setUploadProgress({});
      }, 1500);

    } catch (error) {
      console.error('[Upload] Fatal error:', error);
      toast.error('上传过程中发生错误', {
        description: error instanceof Error ? error.message : '请稍后重试',
      });
    } finally {
      console.log('[Upload] Upload process finished');
      setUploading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        {/* Backdrop */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
          className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        />

        {/* Dialog */}
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          className="relative w-full max-w-3xl max-h-[90vh] overflow-hidden glass-strong rounded-2xl border border-[rgba(56,189,248,0.3)] shadow-[0_0_50px_rgba(56,189,248,0.3)]"
        >
          {/* Header */}
          <div className="flex items-center justify-between p-6 border-b border-[rgba(56,189,248,0.15)]">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center">
                <Upload size={20} className="text-[#0F172A]" />
              </div>
              <h2 className="text-xl text-[#E2E8F0]">上传文档</h2>
            </div>
            <button
              onClick={onClose}
              className="w-8 h-8 rounded-lg hover:bg-[rgba(56,189,248,0.1)] transition-colors flex items-center justify-center text-[#94A3B8] hover:text-[#E2E8F0]"
            >
              <X size={20} />
            </button>
          </div>

          {/* Content */}
          <div className="p-6 overflow-y-auto max-h-[calc(90vh-180px)]">
            <div className="space-y-6">
              {/* Knowledge Base Selection */}
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <label className="text-[#E2E8F0] flex items-center gap-2">
                    <FileText size={16} />
                    选择知识库
                  </label>
                  <motion.button
                    onClick={() => setShowCreateKB(!showCreateKB)}
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    className="flex items-center gap-1 px-3 py-1.5 text-sm border border-[#38BDF8] text-[#38BDF8] rounded-lg hover:bg-[rgba(56,189,248,0.1)] transition-all"
                  >
                    <Plus size={14} />
                    新建知识库
                  </motion.button>
                </div>

                {/* Create KB Input */}
                <AnimatePresence>
                  {showCreateKB && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      exit={{ opacity: 0, height: 0 }}
                      className="overflow-hidden"
                    >
                      <div className="flex gap-2 p-3 glass rounded-xl border border-[rgba(56,189,248,0.2)]">
                        <input
                          type="text"
                          value={newKBName}
                          onChange={(e) => setNewKBName(e.target.value)}
                          placeholder="输入知识库名称（支持中文）"
                          className="flex-1 px-3 py-2 glass-strong border border-[rgba(56,189,248,0.2)] rounded-lg focus:outline-none focus:ring-2 focus:ring-[#38BDF8] text-[#E2E8F0] placeholder-[#94A3B8]"
                          onKeyDown={(e) => e.key === 'Enter' && handleCreateKB()}
                          autoFocus
                        />
                        <motion.button
                          onClick={handleCreateKB}
                          whileHover={{ scale: 1.05 }}
                          whileTap={{ scale: 0.95 }}
                          className="px-4 py-2 bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] rounded-lg hover:shadow-[0_0_20px_rgba(56,189,248,0.5)] transition-all"
                        >
                          创建
                        </motion.button>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>

                {/* KB List */}
                {loading ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 size={24} className="text-[#38BDF8] animate-spin" />
                  </div>
                ) : knowledgeBases.length === 0 ? (
                  <div className="text-center py-8 text-[#94A3B8]">
                    暂无知识库，请先创建一个
                  </div>
                ) : (
                  <div className="grid grid-cols-3 gap-3">
                    {knowledgeBases.map((kb) => {
                      const recommended = usesV2Extraction === kb.isV2;
                      return (
                      <motion.button
                        key={kb.collection_id}
                        onClick={() => setSelectedKB(kb.collection_id)}
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                        className={`p-4 rounded-xl border-2 transition-all text-left ${
                          selectedKB === kb.collection_id
                            ? 'border-[#38BDF8] bg-[rgba(56,189,248,0.1)]'
                            : 'border-[rgba(56,189,248,0.2)] glass hover:border-[rgba(56,189,248,0.4)]'
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2 mb-1">
                          <div
                            className={`truncate ${
                              selectedKB === kb.collection_id ? 'text-[#38BDF8]' : 'text-[#E2E8F0]'
                            }`}
                            title={kb.displayName}
                          >
                            {kb.displayName}
                          </div>
                          <span
                            className={`text-[10px] px-2 py-0.5 rounded-full border ${
                              kb.isV2
                                ? 'border-blue-400 text-blue-300 bg-blue-500/10'
                                : 'border-emerald-400 text-emerald-300 bg-emerald-500/10'
                            }`}
                          >
                            {kb.isV2 ? 'OCR v2' : 'VLM v1'}
                          </span>
                        </div>
                        <div className="text-[11px] text-[#64748B] mb-2 truncate" title={kb.collection_name}>
                          内部名称：{kb.collection_name}
                        </div>
                        <div className="text-xs text-[#94A3B8] flex items-center justify-between">
                          <span>{kb.total_documents} 文档 · {kb.total_chunks} chunks</span>
                          {!recommended && (
                            <span className="text-[10px] text-[#ffb800]">跨模式</span>
                          )}
                        </div>
                      </motion.button>
                    )})}
                  </div>
                )}
              </div>

              {/* File Upload Area */}
              <div className="space-y-3">
                <label className="text-[#E2E8F0]">选择文件</label>
                <div
                  onDrop={handleDrop}
                  onDragOver={(e) => e.preventDefault()}
                  className="border-2 border-dashed border-[rgba(56,189,248,0.3)] rounded-xl p-8 text-center glass hover:border-[#38BDF8] transition-all cursor-pointer"
                >
                  <input
                    type="file"
                    multiple
                    onChange={handleFileSelect}
                    className="hidden"
                    id="file-upload"
                    accept=".pdf,.md,.docx,.jpg,.jpeg,.png"
                  />
                  <label htmlFor="file-upload" className="cursor-pointer">
                    <Upload size={48} className="mx-auto mb-4 text-[#38BDF8]" />
                    <p className="text-[#E2E8F0] mb-2">点击或拖拽文件到此处</p>
                    <p className="text-sm text-[#94A3B8]">目前仅支持 PDF 格式</p>
                  </label>
                </div>

                {/* Selected Files */}
                {files.length > 0 && (
                  <div className="space-y-2">
                    {files.map((file, index) => {
                      const status = uploadProgress[file.name];
                      return (
                        <div
                          key={index}
                          className="flex items-center justify-between p-3 glass rounded-lg border border-[rgba(56,189,248,0.2)]"
                        >
                          <div className="flex items-center gap-3">
                            {status === 'uploading' && (
                              <Loader2 size={16} className="text-[#38BDF8] animate-spin" />
                            )}
                            {status === 'success' && (
                              <CheckCircle size={16} className="text-[#34D399]" />
                            )}
                            {status === 'error' && (
                              <AlertCircle size={16} className="text-[#ff3b5c]" />
                            )}
                            {!status && <FileText size={16} className="text-[#38BDF8]" />}
                            <span className="text-[#E2E8F0]">{file.name}</span>
                            <span className="text-xs text-[#94A3B8]">
                              {(file.size / 1024 / 1024).toFixed(2)} MB
                            </span>
                          </div>
                          {!uploading && (
                            <button
                              onClick={() => setFiles(files.filter((_, i) => i !== index))}
                              className="text-[#94A3B8] hover:text-[#ff3b5c] transition-colors"
                            >
                              <X size={16} />
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Advanced Settings Toggle */}
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className={`flex items-center gap-2 transition-colors ${
                  useLightTheme
                    ? 'text-blue-600 hover:text-blue-700'
                    : 'text-[#38BDF8] hover:text-[#E2E8F0]'
                }`}
              >
                <Settings size={16} />
                {showAdvanced ? '隐藏' : '显示'}高级配置
              </button>

              {/* Advanced Settings */}
              <AnimatePresence>
                {showAdvanced && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    className="space-y-6 overflow-hidden"
                  >
                    {/* Extraction Mode */}
                    <div className="space-y-3">
                      <label className={useLightTheme ? 'text-slate-700 font-medium' : 'text-[#E2E8F0]'}>
                        提取模式
                        {usesV2Extraction && <span className="ml-2 text-xs text-blue-600">(OCR 2.0)</span>}
                      </label>
                      <div className={`grid ${extractionMethods.length === 2 ? 'grid-cols-2' : 'grid-cols-3'} gap-3`}>
                        {extractionMethods.map((method) => (
                          <button
                            key={method.id}
                            onClick={() => setUploadConfig({ ...uploadConfig, extractionMode: method.id })}
                            className={`p-4 rounded-xl border-2 transition-all text-left ${
                              useLightTheme
                                ? uploadConfig.extractionMode === method.id
                                  ? 'border-blue-500 bg-blue-50'
                                  : 'border-slate-200 bg-white hover:border-blue-300'
                                : uploadConfig.extractionMode === method.id
                                  ? 'border-[#38BDF8] bg-[rgba(56,189,248,0.1)]'
                                  : 'border-[rgba(56,189,248,0.2)] glass'
                            }`}
                          >
                            <div
                              className={
                                useLightTheme
                                  ? uploadConfig.extractionMode === method.id
                                    ? 'text-blue-600 font-medium'
                                    : 'text-slate-700'
                                  : uploadConfig.extractionMode === method.id
                                    ? 'text-[#38BDF8]'
                                    : 'text-[#E2E8F0]'
                              }
                            >
                              {method.label}
                            </div>
                            <div className={`text-xs mt-1 ${useLightTheme ? 'text-slate-500' : 'text-[#94A3B8]'}`}>
                              {method.description}
                            </div>
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* Chunking Parameters */}
                    <div className="space-y-3">
                      <label className={useLightTheme ? 'text-slate-700 font-medium' : 'text-[#E2E8F0]'}>切分参数</label>
                      <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                          <label className={`text-sm ${useLightTheme ? 'text-slate-600' : 'text-[#94A3B8]'}`}>Chunk Size</label>
                          <div className="flex gap-2">
                            <input
                              type="number"
                              value={uploadConfig.chunkSize}
                              onChange={(e) =>
                                setUploadConfig({
                                  ...uploadConfig,
                                  chunkSize: Number.parseInt(e.target.value, 10),
                                })
                              }
                              className={`flex-1 px-3 py-2 rounded-lg focus:outline-none focus:ring-2 transition-all ${
                                useLightTheme
                                  ? 'bg-white border border-slate-200 text-slate-700 focus:ring-blue-500 focus:border-blue-500'
                                  : 'glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0] focus:ring-[#38BDF8]'
                              }`}
                            />
                            <span className={`px-3 py-2 rounded-lg text-sm ${useLightTheme ? 'bg-slate-100 text-slate-600' : 'glass text-[#94A3B8]'}`}>tokens</span>
                          </div>
                        </div>

                        <div className="space-y-2">
                          <label className={`text-sm ${useLightTheme ? 'text-slate-600' : 'text-[#94A3B8]'}`}>Overlap</label>
                          <div className="flex gap-2">
                            <input
                              type="number"
                              value={uploadConfig.overlap}
                              onChange={(e) =>
                                setUploadConfig({
                                  ...uploadConfig,
                                  overlap: Number.parseInt(e.target.value, 10),
                                })
                              }
                              className={`flex-1 px-3 py-2 rounded-lg focus:outline-none focus:ring-2 transition-all ${
                                useLightTheme
                                  ? 'bg-white border border-slate-200 text-slate-700 focus:ring-blue-500 focus:border-blue-500'
                                  : 'glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0] focus:ring-[#38BDF8]'
                              }`}
                            />
                            <span className={`px-3 py-2 rounded-lg text-sm ${useLightTheme ? 'bg-slate-100 text-slate-600' : 'glass text-[#94A3B8]'}`}>tokens</span>
                          </div>
                        </div>

                        <div className="space-y-2">
                          <label className={`text-sm ${useLightTheme ? 'text-slate-600' : 'text-[#94A3B8]'}`}>Max Page Span</label>
                          <div className="flex gap-2">
                            <input
                              type="number"
                              value={uploadConfig.maxPageSpan}
                              onChange={(e) =>
                                setUploadConfig({
                                  ...uploadConfig,
                                  maxPageSpan: Number.parseInt(e.target.value, 10),
                                })
                              }
                              className={`flex-1 px-3 py-2 rounded-lg focus:outline-none focus:ring-2 transition-all ${
                                useLightTheme
                                  ? 'bg-white border border-slate-200 text-slate-700 focus:ring-blue-500 focus:border-blue-500'
                                  : 'glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0] focus:ring-[#38BDF8]'
                              }`}
                            />
                            <span className={`px-3 py-2 rounded-lg text-sm ${useLightTheme ? 'bg-slate-100 text-slate-600' : 'glass text-[#94A3B8]'}`}>pages</span>
                          </div>
                        </div>

                        <div className="space-y-2">
                          <label className={`text-sm ${useLightTheme ? 'text-slate-600' : 'text-[#94A3B8]'}`}>Bridge Length</label>
                          <div className="flex gap-2">
                            <input
                              type="number"
                              value={uploadConfig.bridgeLength}
                              onChange={(e) =>
                                setUploadConfig({
                                  ...uploadConfig,
                                  bridgeLength: Number.parseInt(e.target.value, 10),
                                })
                              }
                              className={`flex-1 px-3 py-2 rounded-lg focus:outline-none focus:ring-2 transition-all ${
                                useLightTheme
                                  ? 'bg-white border border-slate-200 text-slate-700 focus:ring-blue-500 focus:border-blue-500'
                                  : 'glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0] focus:ring-[#38BDF8]'
                              }`}
                            />
                            <span className={`px-3 py-2 rounded-lg text-sm ${useLightTheme ? 'bg-slate-100 text-slate-600' : 'glass text-[#94A3B8]'}`}>tokens</span>
                          </div>
                        </div>
                      </div>

                      <div className="space-y-2">
                        <label className={`text-sm ${useLightTheme ? 'text-slate-600' : 'text-[#94A3B8]'}`}>切分方法</label>
                        <select
                          value={uploadConfig.chunkingMethod}
                          onChange={(e) => setUploadConfig({ ...uploadConfig, chunkingMethod: e.target.value })}
                          className={`w-full px-3 py-2 rounded-lg focus:outline-none focus:ring-2 transition-all ${
                            useLightTheme
                              ? 'bg-white border border-slate-200 text-slate-700 focus:ring-blue-500 focus:border-blue-500' 
                              : 'glass-strong border border-[rgba(56,189,248,0.2)] text-[#E2E8F0] bg-[rgba(30,41,59,0.6)] focus:ring-[#38BDF8]'
                          }`}
                        >
                          {usesV2Extraction ? (
                            <>
                              <option value="ocr_aware">OCR感知切分</option>
                              <option value="layout_based">版面感知切分</option>
                            </>
                          ) : (
                            <>
                              <option value="header_recursive">递归标题分割</option>
                              <option value="markdown_only">自定义Markdown分割</option>
                            </>
                          )}
                        </select>
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              {/* Warning */}
              {!selectedKB && files.length > 0 && (
                <div className="flex items-center gap-2 p-3 glass rounded-lg border border-[rgba(255,184,0,0.3)] bg-[rgba(255,184,0,0.05)]">
                  <AlertCircle size={16} className="text-[#ffb800]" />
                  <span className="text-sm text-[#ffb800]">请先选择一个知识库</span>
                </div>
              )}
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-3 p-6 border-t border-[rgba(56,189,248,0.15)]">
            <motion.button
              onClick={onClose}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
              className="px-6 py-3 glass border border-[rgba(56,189,248,0.2)] rounded-xl hover:bg-[rgba(56,189,248,0.05)] transition-all text-[#E2E8F0]"
            >
              取消
            </motion.button>
            <motion.button
              onClick={handleSubmit}
              disabled={!selectedKB || files.length === 0 || uploading}
              whileHover={selectedKB && files.length > 0 && !uploading ? { scale: 1.05 } : {}}
              whileTap={selectedKB && files.length > 0 && !uploading ? { scale: 0.95 } : {}}
              className={`px-6 py-3 rounded-xl transition-all relative overflow-hidden group flex items-center gap-2 ${
                selectedKB && files.length > 0 && !uploading
                  ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] hover:shadow-[0_0_30px_rgba(56,189,248,0.6)]'
                  : 'bg-[rgba(56,189,248,0.2)] text-[#94A3B8] cursor-not-allowed'
              }`}
            >
              {selectedKB && files.length > 0 && !uploading && (
                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent opacity-0 group-hover:opacity-20 shimmer" />
              )}
              {uploading && <Loader2 size={16} className="animate-spin" />}
              <span className="relative z-10">
                {uploading ? '上传中...' : `上传 (${files.length})`}
              </span>
            </motion.button>
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  );
}
