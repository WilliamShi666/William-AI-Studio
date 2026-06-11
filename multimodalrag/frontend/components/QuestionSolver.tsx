import { useEffect, useMemo, useRef, useState, type MouseEvent, type DragEvent, type ClipboardEvent } from 'react';
import { motion } from 'motion/react';
import { Bot, ChevronLeft, ChevronRight, Loader2, MessageSquare, Plus, Send, Square, Trash2, User, Brain, Paperclip, X, FileText, Image as ImageIcon, GraduationCap } from 'lucide-react';
import { toast } from 'sonner';
import ReactMarkdown from 'react-markdown';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';
import { markdownRemarkPlugins, markdownRehypePlugins, normalizeMathDelimiters } from './markdownPlugins';
import { markdownTableComponents } from './markdownTableComponents';
import { ModeId } from '../src/modes';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type UploadKind = 'image' | 'pdf';

interface UploadItem {
  id: string;
  name: string;
  size: number;
  type: string;
  kind: UploadKind;
  dataUrl: string;
  previewUrl?: string;
}

interface QaModel {
  key: string;
  model_id: string;
  display_name: string;
  provider: string;
  supports_pdf: boolean;
  supports_thinking: boolean;
  supports_vision: boolean;
}

interface QaSessionApi {
  session_id: string;
  title: string | null;
  model_name: string | null;
  created_at: string;
  updated_at: string;
}

interface QaMessageApi {
  message_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  images?: string[] | null;
  files?: Array<{ filename: string; data?: string }> | null;
  reasoning_content?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
}

interface QaSession {
  id: string;
  title: string;
  modelKey: string | null;
  createdAt: string;
  updatedAt: string;
}

interface QaMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  images?: string[];
  files?: Array<{ filename: string; data?: string }>;
  reasoning?: string;
  isStreaming?: boolean;
  isThinking?: boolean;  // 是否正在思考（用于兜底显示）
}

interface QaModelsResponse {
  status: 'success' | 'error';
  models: QaModel[];
}

interface QaSessionsResponse {
  status: 'success' | 'error';
  sessions: QaSessionApi[];
}

interface QaSessionDetailResponse {
  status: 'success' | 'error';
  session: QaSessionApi;
  messages: QaMessageApi[];
}

interface QaChatStreamChunk {
  type: 'content' | 'reasoning' | 'session' | 'error' | 'end';
  data?: any;
}

interface QuestionSolverProps {
  mode: ModeId;
  isHistoryOpen: boolean;
  onToggleHistory: () => void;
  onSetHistoryOpen: (isOpen: boolean) => void;
}

const formatTimestamp = (value: string) =>
  new Date(value).toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });

const normalizeSession = (session: QaSessionApi): QaSession => ({
  id: session.session_id,
  title: session.title || '未命名会话',
  modelKey: session.model_name || null,
  createdAt: session.created_at,
  updatedAt: session.updated_at,
});

const readFileAsDataUrl = (file: File): Promise<string> => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
};

// 经济答题区题型配置
type AnswerZone = 'general' | 'economics';

interface EconQuestionType {
  key: string;
  label: string;
  group: 'AS' | 'A2';
}

const ECON_QUESTION_TYPES: EconQuestionType[] = [
  { key: 'as_data_response', label: 'CIE AS Data Response', group: 'AS' },
  { key: 'as_8_marker', label: 'CIE AS 8 Marker', group: 'AS' },
  { key: 'as_12_marker', label: 'CIE AS 12 Marker', group: 'AS' },
  { key: 'full_as_paper_2', label: 'Full CIE AS Paper 2', group: 'AS' },
  { key: 'a2_data_response', label: 'CIE A2 Data Response', group: 'A2' },
  { key: 'a2_20_marker', label: 'CIE A2 20 Marker', group: 'A2' },
  { key: 'full_a2_paper_4', label: 'Full CIE A2 Paper 4', group: 'A2' },
];

export function QuestionSolver({ mode, isHistoryOpen, onToggleHistory, onSetHistoryOpen }: QuestionSolverProps) {
  const [message, setMessage] = useState('');
  const [messages, setMessages] = useState<QaMessage[]>([]);
  const [sessions, setSessions] = useState<QaSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [models, setModels] = useState<QaModel[]>([]);
  const [selectedModelKey, setSelectedModelKey] = useState<string>('');
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [enableThinking, setEnableThinking] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [answerZone, setAnswerZone] = useState<AnswerZone>('general');
  const [questionType, setQuestionType] = useState<string>('');
  const [socraticMode, setSocraticMode] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const isUserNearBottomRef = useRef(true);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const modelResponse = await authFetch(`${config.chatApiUrl}/qa/models`);
        const modelResult = (await modelResponse.json()) as QaModelsResponse;
        if (modelResult.status === 'success') {
          setModels(modelResult.models || []);
          const savedModel = localStorage.getItem('qa_model_key');
          const defaultModel = modelResult.models.find((item) => item.key === savedModel) || modelResult.models[0];
          if (defaultModel) {
            setSelectedModelKey(defaultModel.key);
          }
        }
      } catch (error) {
        console.error('加载模型失败:', error);
        toast.error('模型加载失败');
      }

      try {
        const sessionResponse = await authFetch(`${config.chatApiUrl}/qa/sessions`);
        const sessionResult = (await sessionResponse.json()) as QaSessionsResponse;
        if (sessionResult.status === 'success') {
          const normalized = sessionResult.sessions.map(normalizeSession);
          setSessions(normalized);
        }
      } catch (error) {
        console.error('加载会话失败:', error);
        toast.error('会话加载失败');
      }
    };

    fetchData();
  }, [mode]);

  useEffect(() => {
    const handleResponsiveCollapse = () => {
      if (window.innerWidth < 1200) {
        onSetHistoryOpen(false);
      }
    };
    handleResponsiveCollapse();
    window.addEventListener('resize', handleResponsiveCollapse);
    return () => window.removeEventListener('resize', handleResponsiveCollapse);
  }, [onSetHistoryOpen]);

  useEffect(() => {
    if (selectedModelKey) {
      localStorage.setItem('qa_model_key', selectedModelKey);
    }
  }, [selectedModelKey]);

  const selectedModel = useMemo(
    () => models.find((model) => model.key === selectedModelKey) || null,
    [models, selectedModelKey],
  );

  useEffect(() => {
    if (!selectedModel?.supports_pdf) {
      setUploads((prev) => prev.filter((item) => item.kind !== 'pdf'));
    }
  }, [selectedModel?.supports_pdf]);

  useEffect(() => {
    if (!selectedModel?.supports_thinking) {
      setEnableThinking(false);
    }
  }, [selectedModel?.supports_thinking]);

  const scrollToBottom = () => {
    const container = messagesContainerRef.current;
    if (!container) return;
    // 只在用户位于底部附近时自动滚动，允许用户自由向上查看历史
    if (isUserNearBottomRef.current) {
      container.scrollTop = container.scrollHeight;
    }
  };

  const isAnyMessageStreaming = messages.some((msg) => msg.isStreaming);

  useEffect(() => {
    scrollToBottom();
  }, [messages, isAnyMessageStreaming]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      isUserNearBottomRef.current = scrollHeight - scrollTop - clientHeight < 100;
    };

    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, []);

  const loadSession = async (sessionId: string) => {
    try {
      const response = await authFetch(`${config.chatApiUrl}/qa/sessions/${sessionId}`);
      const result = (await response.json()) as QaSessionDetailResponse;
      if (result.status !== 'success') {
        toast.error('加载会话失败');
        return;
      }

      const session = normalizeSession(result.session);
      setCurrentSessionId(session.id);
      setSelectedModelKey(session.modelKey || selectedModelKey);

      const mappedMessages: QaMessage[] = result.messages.map((msg) => ({
        id: msg.message_id,
        role: msg.role === 'assistant' ? 'assistant' : 'user',
        content: msg.content,
        images: msg.images || undefined,
        files: msg.files || undefined,
        reasoning: msg.reasoning_content || undefined,
        timestamp: formatTimestamp(msg.created_at),
      }));

      setMessages(mappedMessages);
      toast.success(`已加载会话: ${session.title}`);
    } catch (error) {
      console.error('加载会话失败:', error);
      toast.error('加载会话失败');
    }
  };

  const handleNewSession = () => {
    setCurrentSessionId(null);
    setMessages([]);
    setUploads([]);
    setMessage('');
    setQuestionType('');  // 重置题型选择
    toast.success('已创建新会话');
  };

  const handleDeleteSession = async (sessionId: string, event: MouseEvent) => {
    event.stopPropagation();
    try {
      await authFetch(`${config.chatApiUrl}/qa/sessions/${sessionId}`, { method: 'DELETE' });
      setSessions((prev) => prev.filter((session) => session.id !== sessionId));
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null);
        setMessages([]);
      }
      toast.success('会话已删除');
    } catch (error) {
      console.error('删除会话失败:', error);
      toast.error('删除会话失败');
    }
  };

  const handleStopGeneration = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  };

  const handleSendMessage = async () => {
    if (isLoading) return;

    const trimmed = message.trim();
    const imageUploads = uploads.filter((item) => item.kind === 'image');
    const pdfUploads = uploads.filter((item) => item.kind === 'pdf');

    if (!trimmed && imageUploads.length === 0 && pdfUploads.length === 0) {
      toast.error('请输入问题或上传图片/PDF');
      return;
    }

    if (pdfUploads.length > 0 && !selectedModel?.supports_pdf) {
      toast.error('当前模型不支持 PDF');
      return;
    }

    if (!selectedModelKey) {
      toast.error('请先选择模型');
      return;
    }

    const queryText = trimmed || '请解析附件中的题目';

    const userMessage: QaMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: trimmed || '(仅上传文件)',
      timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      images: imageUploads.map((item) => item.dataUrl),
      files: pdfUploads.map((item) => ({ filename: item.name, data: item.dataUrl })),
    };

    setMessages((prev) => [...prev, userMessage]);
    setMessage('');
    setUploads([]);
    setIsLoading(true);

    const assistantMessage: QaMessage = {
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      content: '',
      timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      isStreaming: true,
      isThinking: enableThinking,  // 启用思考模式时显示"正在思考..."
    };
    setMessages((prev) => [...prev, assistantMessage]);

    try {
      abortControllerRef.current = new AbortController();

      const response = await authFetch(`${config.chatApiUrl}/qa/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: queryText,
          model_key: selectedModelKey,
          session_id: currentSessionId,
          images: userMessage.images,
          files: userMessage.files,
          enable_thinking: enableThinking,
          stream: true,
          question_type: socraticMode
            ? 'qa_socratic_method'
            : (answerZone === 'economics' && questionType ? questionType : null),
        }),
        signal: abortControllerRef.current.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(`HTTP error ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulatedContent = '';
      let accumulatedReasoning = '';
      let activeSessionId = currentSessionId;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line) as QaChatStreamChunk;
            if (data.type === 'session' && data.data) {
              const session = normalizeSession(data.data as QaSessionApi);
              activeSessionId = session.id;
              setCurrentSessionId(session.id);
              setSessions((prev) => {
                const next = [session, ...prev.filter((item) => item.id !== session.id)];
                return next;
              });
            }
            if (data.type === 'reasoning') {
              accumulatedReasoning += data.data || '';
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessage.id
                    ? { ...msg, reasoning: accumulatedReasoning, isThinking: false }
                    : msg,
                ),
              );
            }
            if (data.type === 'content') {
              accumulatedContent += data.data || '';
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMessage.id
                    ? { ...msg, content: accumulatedContent, isThinking: false }
                    : msg,
                ),
              );
            }
            if (data.type === 'error') {
              toast.error(data.data?.error || '模型调用失败');
            }
          } catch (error) {
            console.warn('解析响应失败:', line, error);
          }
        }
      }

      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantMessage.id
            ? { ...msg, isStreaming: false, isThinking: false }
            : msg,
        ),
      );

      if (activeSessionId) {
        setSessions((prev) =>
          prev.map((session) =>
            session.id === activeSessionId
              ? { ...session, updatedAt: new Date().toISOString() }
              : session,
          ),
        );
      }
    } catch (error: unknown) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        // 用户主动中断，保留已生成的内容
        console.log('用户中断生成');
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMessage.id
              ? { ...msg, isStreaming: false, isThinking: false }
              : msg
          )
        );
      } else {
        console.error('对话失败:', error);
        toast.error('对话失败，请稍后重试');
        setMessages((prev) => prev.filter((msg) => msg.id !== assistantMessage.id));
      }
    } finally {
      setIsLoading(false);
      abortControllerRef.current = null;
    }
  };

  const handleKeyPress = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void handleSendMessage();
    }
  };

  const handleFileUpload = async (fileList: FileList | File[] | null) => {
    if (!fileList || isLoading) return;

    const allowPdf = Boolean(selectedModel?.supports_pdf);
    const files = Array.from(fileList).filter((file) => {
      if (file.type.startsWith('image/')) return true;
      if (allowPdf && file.type === 'application/pdf') return true;
      return false;
    });

    if (files.length === 0) return;

    const items = await Promise.all(
      files.map(async (file) => {
        const dataUrl = await readFileAsDataUrl(file);
        const isImage = file.type.startsWith('image/');
        return {
          id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
          name: file.name,
          size: file.size,
          type: file.type,
          kind: isImage ? 'image' : 'pdf',
          dataUrl,
          previewUrl: isImage ? dataUrl : undefined,
        } as UploadItem;
      }),
    );

    setUploads((prev) => [...prev, ...items]);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
    if (isLoading) return;
    void handleFileUpload(event.dataTransfer.files);
  };

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
  };

  const handlePaste = async (event: ClipboardEvent<HTMLTextAreaElement>) => {
    if (isLoading) return;

    const items = event.clipboardData.items;
    const imageFiles: File[] = [];

    for (const item of items) {
      if (item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) {
          imageFiles.push(file);
        }
      }
    }

    if (imageFiles.length > 0) {
      await handleFileUpload(imageFiles);
    }
  };

  const handleRemoveUpload = (id: string) => {
    setUploads((prev) => prev.filter((item) => item.id !== id));
  };

  const handleAttachClick = () => {
    if (isLoading) return;
    fileInputRef.current?.click();
  };

  const sessionListWidth = isHistoryOpen ? 280 : 56;

  return (
    <div className="flex h-[calc(100vh-64px)]">
      <div
        className="glass-strong flex flex-col transition-[width] duration-300 ease-in-out overflow-hidden border-r border-[rgba(56,189,248,0.15)]"
        style={{ width: `${sessionListWidth}px` }}
      >
        {isHistoryOpen ? (
          <>
            <div className="p-4 border-b border-[rgba(56,189,248,0.15)] flex items-center justify-between">
              <div className="flex items-center gap-2">
                <MessageSquare size={18} className="text-[#38BDF8]" />
                <h3 className="text-[#E2E8F0]">会话历史</h3>
              </div>
              <div className="flex items-center gap-2">
                <motion.button
                  onClick={handleNewSession}
                  className="w-8 h-8 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                  aria-label="新建会话"
                >
                  <Plus size={18} className="text-[#38BDF8]" />
                </motion.button>
                <motion.button
                  onClick={onToggleHistory}
                  className="w-8 h-8 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                  aria-label="收起会话历史"
                >
                  <ChevronLeft size={18} className="text-[#38BDF8]" />
                </motion.button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-2 space-y-2">
              {sessions.length === 0 ? (
                <div className="text-[#94A3B8] text-sm text-center py-4">暂无历史会话</div>
              ) : (
                sessions.map((session) => (
                  <motion.div
                    key={session.id}
                    onClick={() => void loadSession(session.id)}
                    className={`p-3 rounded-lg cursor-pointer transition-all group ${
                      currentSessionId === session.id
                        ? 'bg-[rgba(56,189,248,0.15)] border border-[rgba(56,189,248,0.3)]'
                        : 'glass hover:bg-[rgba(56,189,248,0.1)]'
                    }`}
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <MessageSquare size={14} className="text-[#38BDF8] flex-shrink-0" />
                          <p className="text-[#E2E8F0] text-sm font-medium truncate">{session.title}</p>
                        </div>
                        <p className="text-[#94A3B8] text-xs truncate">
                          {models.find((model) => model.key === session.modelKey)?.display_name || session.modelKey || '未选择模型'}
                        </p>
                        <p className="text-[#64748B] text-xs mt-1">{formatTimestamp(session.updatedAt)}</p>
                      </div>
                      <motion.button
                        onClick={(event) => handleDeleteSession(session.id, event)}
                        className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-500/20 rounded transition-all"
                        whileHover={{ scale: 1.1 }}
                        whileTap={{ scale: 0.9 }}
                      >
                        <Trash2 size={14} className="text-red-400" />
                      </motion.button>
                    </div>
                  </motion.div>
                ))
              )}
            </div>
          </>
        ) : (
          <div className="flex flex-col items-center gap-3 py-4">
            <motion.button
              onClick={handleNewSession}
              className="w-10 h-10 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center"
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
              aria-label="新建会话"
            >
              <Plus size={20} className="text-[#38BDF8]" />
            </motion.button>
            <motion.button
              onClick={onToggleHistory}
              className="w-10 h-10 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center"
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
              aria-label="展开会话历史"
            >
              <ChevronRight size={20} className="text-[#38BDF8]" />
            </motion.button>
          </div>
        )}
      </div>

      <div className="flex-1 flex flex-col">
        <div className="border-b border-[rgba(56,189,248,0.15)] px-6 py-4 flex flex-col gap-4">
          {/* Zone Selector Tabs */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => setAnswerZone('general')}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                answerZone === 'general'
                  ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A]'
                  : 'glass text-[#94A3B8] hover:text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.1)]'
              }`}
            >
              通用答题区
            </button>
            <button
              onClick={() => setAnswerZone('economics')}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                answerZone === 'economics'
                  ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A]'
                  : 'glass text-[#94A3B8] hover:text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.1)]'
              }`}
            >
              经济答题区
            </button>
          </div>

          {/* Header Row - 标题、模型、思考模式横排 */}
          <div className="flex items-center gap-6">
            {/* 左侧：图标 + 标题 + 副标题 */}
            <div className="flex items-center gap-2">
              <Bot size={20} className="text-[#38BDF8]" />
              <div>
                <span className="text-[#E2E8F0] font-semibold">
                  {answerZone === 'general' ? '破题结界' : '经济答题区'}
                </span>
                <div className="text-xs text-[#94A3B8]">
                  {answerZone === 'general'
                    ? '上传题目图片或 PDF，获得解析思路'
                    : '专注A-Level经济写作，获取专业答案'}
                </div>
              </div>
            </div>

            {/* 模型选择 + 思考模式 + 经济题型 */}
            <div className="flex items-center gap-4">
              {/* 模型选择 */}
              <Select value={selectedModelKey} onValueChange={setSelectedModelKey}>
                <SelectTrigger className="bg-[#0F172A] text-[#E2E8F0] text-sm rounded-lg border-[rgba(56,189,248,0.2)] px-3 py-2 h-auto w-auto min-w-[140px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-[rgba(15,23,36,0.98)] border-[rgba(56,189,248,0.2)] text-[#CBD5E1]">
                  {models.map((model) => (
                    <SelectItem key={model.key} value={model.key}>
                      {model.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {/* 思考模式 */}
              <label className="flex items-center gap-2 text-xs text-[#94A3B8] whitespace-nowrap">
                <input
                  type="checkbox"
                  className="accent-[#38BDF8]"
                  checked={enableThinking}
                  onChange={(event) => setEnableThinking(event.target.checked)}
                  disabled={!selectedModel?.supports_thinking}
                />
                思考模式
              </label>

              {/* 经济题型选择（仅在经济答题区显示） */}
              {answerZone === 'economics' && (
                <select
                  className="bg-[#0F172A] text-[#E2E8F0] text-sm rounded-lg border border-[rgba(56,189,248,0.2)] px-3 py-2"
                  value={questionType}
                  onChange={(event) => setQuestionType(event.target.value)}
                >
                  <option value="">选择题型...</option>
                  <optgroup label="AS Level">
                    {ECON_QUESTION_TYPES.filter(t => t.group === 'AS').map((type) => (
                      <option key={type.key} value={type.key}>
                        {type.label}
                      </option>
                    ))}
                  </optgroup>
                  <optgroup label="A2 Level">
                    {ECON_QUESTION_TYPES.filter(t => t.group === 'A2').map((type) => (
                      <option key={type.key} value={type.key}>
                        {type.label}
                      </option>
                    ))}
                  </optgroup>
                </select>
              )}
            </div>
          </div>
        </div>

        <div className="flex-1 overflow-hidden flex flex-col">
          <div ref={messagesContainerRef} className="flex-1 overflow-y-auto p-6 space-y-6">
            {messages.length === 0 ? (
              <div className="text-center text-[#94A3B8] text-sm">
                还没有提问，上传题目开始解析吧。
              </div>
            ) : (
              messages.map((msg) => (
                <div key={msg.id} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  {msg.role === 'assistant' && (
                    <div className="w-9 h-9 rounded-full bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center">
                      <Bot size={18} className="text-[#0F172A]" />
                    </div>
                  )}
                  <div className={`max-w-[760px] rounded-2xl px-4 py-3 ${msg.role === 'user'
                    ? 'bg-[#38BDF8] text-[#0F172A]'
                    : (mode === 'roys'
                        ? 'bg-transparent border-none shadow-none text-slate-300 backdrop-blur-sm bg-[#132245]/20'
                        : 'glass text-[#E2E8F0]')}`}
                  >
                    <div className="text-xs opacity-70 mb-2 flex items-center gap-2">
                      {msg.role === 'user' ? <User size={12} /> : <Bot size={12} />}
                      <span>{msg.timestamp}</span>
                    </div>
                    {/* 思考过程显示（含兜底） */}
                    {(msg.reasoning || msg.isThinking) && (
                      <details className="mb-3 text-xs text-[#94A3B8]" open={msg.isThinking}>
                        <summary className="cursor-pointer flex items-center gap-2">
                          <Brain size={12} className={msg.isThinking ? 'animate-pulse' : ''} />
                          {msg.isThinking ? '正在思考...' : '思考过程'}
                        </summary>
                        {msg.reasoning ? (
                          <div className="mt-2 whitespace-pre-wrap">{msg.reasoning}</div>
                        ) : (
                          <div className="mt-2 text-[#64748B] italic">思考内容加载中...</div>
                        )}
                      </details>
                    )}
                    {msg.role === 'assistant' ? (
                      <ReactMarkdown
                        remarkPlugins={markdownRemarkPlugins}
                        rehypePlugins={markdownRehypePlugins as any}
                        components={markdownTableComponents}
                      >
                        {normalizeMathDelimiters(msg.content)}
                      </ReactMarkdown>
                    ) : (
                      <p className="whitespace-pre-wrap text-sm">{msg.content}</p>
                    )}

                    {Array.isArray(msg.images) && msg.images.length > 0 && (
                      <div className="mt-3 grid grid-cols-2 gap-3">
                        {msg.images.map((image, index) => (
                          <img
                            key={`${msg.id}-img-${index}`}
                            src={image}
                            alt={`upload-${index}`}
                            className="rounded-lg border border-[rgba(56,189,248,0.2)] object-cover"
                          />
                        ))}
                      </div>
                    )}

                    {Array.isArray(msg.files) && msg.files.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-2 text-xs">
                        {msg.files.map((file, index) => (
                          <div
                            key={`${msg.id}-file-${index}`}
                            className="px-3 py-1 rounded-full bg-[rgba(56,189,248,0.12)] text-[#E2E8F0]"
                          >
                            {file.filename}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  {msg.role === 'user' && (
                    <div className="w-9 h-9 rounded-full bg-[#38BDF8] flex items-center justify-center">
                      <User size={18} className="text-[#0F172A]" />
                    </div>
                  )}
                </div>
              ))
            )}
            {isLoading && (
              <div className="flex items-center gap-2 text-[#94A3B8] text-sm">
                <Loader2 className="animate-spin" size={16} /> 模型推理中...
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="border-t border-[rgba(56,189,248,0.15)] p-6 space-y-4">
            {/* 苏格拉底解题法按钮 - 仅在通用答题区显示 */}
            {answerZone === 'general' && (
              <div className="flex items-center gap-2">
                <motion.button
                  onClick={() => setSocraticMode(!socraticMode)}
                  className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                    socraticMode
                      ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-white shadow-[0_0_15px_rgba(56,189,248,0.4)]'
                      : 'glass text-[#94A3B8] hover:text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.1)] border border-[rgba(56,189,248,0.3)]'
                  }`}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                  title="启用苏格拉底解题法：AI将通过提问引导你理解解题思路"
                >
                  <GraduationCap size={16} />
                  <span>苏格拉底解题法</span>
                  {socraticMode && <span className="w-2 h-2 rounded-full bg-white animate-pulse" />}
                </motion.button>
                {socraticMode && (
                  <span className="text-xs text-[#94A3B8]">
                    已启用：AI将引导你逐步理解解题思路，而非直接给出答案
                  </span>
                )}
              </div>
            )}

            {/* 已上传文件预览 */}
            {uploads.length > 0 && (
              <div className="flex flex-wrap gap-3">
                {uploads.map((file) => (
                  <motion.div
                    key={file.id}
                    className="relative flex items-center gap-2 rounded-lg bg-[rgba(15,23,42,0.6)] border border-[rgba(56,189,248,0.2)] px-3 py-2"
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    {file.kind === 'image' ? (
                      <div className="w-10 h-10 rounded-md overflow-hidden border border-[rgba(56,189,248,0.2)]">
                        {file.previewUrl ? (
                          <img src={file.previewUrl} alt={file.name} className="w-full h-full object-cover" />
                        ) : (
                          <div className="w-full h-full flex items-center justify-center text-[#38BDF8]">
                            <ImageIcon size={16} />
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="w-10 h-10 rounded-md border border-[rgba(56,189,248,0.2)] flex items-center justify-center text-[#38BDF8]">
                        <FileText size={18} />
                      </div>
                    )}
                    <div className="text-xs text-[#E2E8F0] max-w-[160px] truncate">
                      {file.name}
                    </div>
                    <button
                      type="button"
                      onClick={() => handleRemoveUpload(file.id)}
                      className="absolute -top-2 -right-2 w-6 h-6 rounded-full bg-[#0F172A] border border-[rgba(56,189,248,0.3)] flex items-center justify-center hover:bg-red-500/20 transition-colors"
                    >
                      <X size={12} className="text-[#94A3B8]" />
                    </button>
                  </motion.div>
                ))}
              </div>
            )}

            {/* 隐藏的文件输入 */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={selectedModel?.supports_pdf ? 'image/*,application/pdf' : 'image/*'}
              className="hidden"
              onChange={(event) => void handleFileUpload(event.target.files)}
            />

            {/* 输入区域：支持拖拽 */}
            <div
              className={`relative flex items-end gap-3 rounded-xl transition-all ${
                isDragging ? 'ring-2 ring-[#38BDF8] bg-[rgba(56,189,248,0.05)]' : ''
              }`}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onDragEnter={handleDragEnter}
              onDragLeave={handleDragLeave}
            >
              <textarea
                className="flex-1 bg-[#0F172A] text-[#E2E8F0] rounded-xl border border-[rgba(56,189,248,0.2)] px-4 py-3 min-h-[80px] focus:outline-none focus:border-[#38BDF8] resize-none"
                placeholder="输入题目描述或提问...（支持拖拽/粘贴图片）"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                onKeyDown={handleKeyPress}
                onPaste={handlePaste}
                disabled={isLoading}
              />
              <div className="flex flex-col gap-2">
                <motion.button
                  onClick={handleAttachClick}
                  className="w-12 h-12 rounded-xl glass border border-[rgba(56,189,248,0.2)] flex items-center justify-center text-[#38BDF8] hover:bg-[rgba(56,189,248,0.1)]"
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                  disabled={isLoading}
                  title="添加附件"
                >
                  <Paperclip size={18} />
                </motion.button>
                <motion.button
                  onClick={isLoading ? handleStopGeneration : () => void handleSendMessage()}
                  className={`w-12 h-12 rounded-xl flex items-center justify-center ${
                    isLoading
                      ? 'bg-gradient-to-br from-[#EF4444] to-[#DC2626] text-white'
                      : 'bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] text-[#0F172A]'
                  }`}
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                  disabled={!isLoading && !message.trim() && uploads.length === 0}
                  title={isLoading ? '停止生成' : '发送消息'}
                >
                  {isLoading ? <Square size={18} fill="currentColor" /> : <Send size={18} />}
                </motion.button>
              </div>
            </div>

            {/* 拖拽提示覆盖层 */}
            {isDragging && (
              <div className="absolute inset-0 flex items-center justify-center bg-[rgba(15,23,42,0.8)] rounded-xl pointer-events-none">
                <div className="text-[#38BDF8] text-lg font-medium">释放以上传文件</div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
