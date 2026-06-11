import { Plus, ChevronDown, Send, Bot, User, Sparkles, Trash2, MessageSquare, ChevronLeft, ChevronRight, GraduationCap, Square, Brain } from 'lucide-react';
import { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { toast } from 'sonner';
import ReactMarkdown from 'react-markdown';
import { config } from '../src/config';
import { markdownRemarkPlugins, markdownRehypePlugins, normalizeMathDelimiters } from './markdownPlugins';
import { markdownTableComponents } from './markdownTableComponents';
import { ModeId, isModeUsingV2Data } from '../src/modes';
import { authFetch, appendAuthTokenToUrl } from '../src/api/auth';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  sources?: SourceDocument[];
  isStreaming?: boolean;
  reasoning?: string;      // 思考过程
  isThinking?: boolean;    // 是否正在思考
}

interface SourceDocument {
  chunk_text: string;
  filename: string;
  score: number;
  retrieval_score?: number;
  rerank_score?: number;
  metadata: Record<string, unknown>;
}

interface KnowledgeBase {
  collection_id: string;
  display_name: string;
  collection_name?: string;
  created_at: string;
  is_v2?: boolean;
  raw_display_name?: string;
}

interface LLMConfig {
  api_url: string;
  api_key: string;
  model_name: string;
  temperature: number;
  max_tokens: number;
}

interface ModelOption {
  name: string;
  display: string;
  provider: string;
  api_url?: string;
  supports_thinking?: boolean;
}

interface ChatSession {
  id: string;
  title: string;
  messages: Message[];
  knowledgeBaseId: string;
  knowledgeBaseName: string;
  createdAt: string;
  updatedAt: string;
}

interface ChatProps {
  mode: ModeId;
  isHistoryOpen: boolean;
  onToggleHistory: () => void;
  onSetHistoryOpen: (isOpen: boolean) => void;
}

interface KnowledgeBaseListResponse {
  status: 'success' | 'error';
  knowledge_bases: KnowledgeBase[];
}

interface ChatConfigResponse {
  status: 'success' | 'error';
  config: {
    llm: LLMConfig;
    available_models: ModelOption[];
    deepseek?: {
      api_url: string;
      api_key: string;
    };
  };
}

type ChatStreamChunk =
  | { type: 'content'; data: string }
  | { type: 'reasoning'; data: string }
  | { type: 'sources'; data: SourceDocument[] }
  | { type: 'metadata'; data: Record<string, unknown> }
  | { type: 'error'; data: { error: string } };

const isChatSession = (session: unknown): session is ChatSession => {
  if (!session || typeof session !== 'object') {
    return false;
  }

  const candidate = session as Partial<ChatSession>;
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.title === 'string' &&
    Array.isArray(candidate.messages) &&
    typeof candidate.knowledgeBaseId === 'string'
  );
};

const formatKnowledgeBaseLabel = (kb?: KnowledgeBase | null) => {
  if (!kb) return '';
  const base = kb.display_name || kb.collection_name || '未命名';
  const suffix = kb.is_v2 ? 'OCR v2.0.0' : 'VLM v1.0.0';
  return `${base} · ${suffix}`;
};

export function Chat({ mode, isHistoryOpen, onToggleHistory, onSetHistoryOpen }: ChatProps) {
  const [message, setMessage] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [selectedKB, setSelectedKB] = useState<KnowledgeBase | null>(null);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [expandedCitation, setExpandedCitation] = useState<string | null>(null);
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [llmConfig, setLLMConfig] = useState<LLMConfig>({
    api_url: 'https://api.deepseek.com',
    api_key: '',
    model_name: 'deepseek-v4-flash',
    temperature: 0.7,
    max_tokens: 5000
  });
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([]);
  const [socraticMode, setSocraticMode] = useState(false);
  const [deepseekConfig, setDeepseekConfig] = useState<{api_url: string; api_key: string} | null>(null);
  const [dashscopeConfig, setDashscopeConfig] = useState<{api_url: string; api_key: string} | null>(null);
  const [enableThinking, setEnableThinking] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const isUserNearBottomRef = useRef(true);
  const abortControllerRef = useRef<AbortController | null>(null);

  const usesV2Data = isModeUsingV2Data(mode);

  // 加载知识库列表和默认配置
  useEffect(() => {
    const fetchData = async () => {
      try {
        // 加载知识库列表
        const kbResponse = await authFetch(`${config.milvusApiUrl}/knowledge_base/list`);
        const kbResult = (await kbResponse.json()) as KnowledgeBaseListResponse;

        if (
          kbResult.status === 'success' &&
          Array.isArray(kbResult.knowledge_bases) &&
          kbResult.knowledge_bases.length > 0
        ) {
          const processedKBs = kbResult.knowledge_bases.map((kb: KnowledgeBase) => {
            const rawName = kb.display_name || kb.collection_name || '';
            const isV2KB = rawName.endsWith('_v2');
            return {
              ...kb,
              raw_display_name: rawName,
              display_name: isV2KB ? rawName.slice(0, -3) : rawName,
              is_v2: isV2KB,
            };
          });
          
          setKnowledgeBases(processedKBs);
          if (processedKBs.length > 0) {
            setSelectedKB(prev => {
              if (prev) {
                const existing = processedKBs.find(k => k.collection_id === prev.collection_id);
                if (existing) {
                  return existing;
                }
              }
              const preferred = processedKBs.find(kb => Boolean(kb.is_v2) === usesV2Data);
              return preferred ?? processedKBs[0];
            });
          }
        }

        // 加载默认LLM配置
        const configResponse = await authFetch(`${config.chatApiUrl}/config/default`);
        const configResult = (await configResponse.json()) as ChatConfigResponse;

        if (configResult.status === 'success' && configResult.config) {
          setLLMConfig(configResult.config.llm);
          setAvailableModels(configResult.config.available_models);
          // 保存 DashScope 配置，用于模型切换时恢复
          setDashscopeConfig({
            api_url: configResult.config.llm.api_url,
            api_key: configResult.config.llm.api_key,
          });
          if (configResult.config.deepseek) {
            setDeepseekConfig(configResult.config.deepseek);
          }
        }

        // 从localStorage加载对话历史
        const savedSessions = localStorage.getItem('chat_sessions');
        if (savedSessions) {
          const parsedSessions = JSON.parse(savedSessions) as unknown;
          if (Array.isArray(parsedSessions)) {
            const sessions = parsedSessions.filter(isChatSession);
            setChatSessions(sessions.sort((a, b) =>
              new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()
            ));
          }
        }
      } catch (error: unknown) {
        console.error('加载数据失败:', error);
        toast.error('加载配置失败');
      }
    };

    fetchData();
  }, [usesV2Data]);

  // 自动滚动到底部（智能滚动策略）
  const scrollToBottom = () => {
    const container = messagesContainerRef.current;
    if (!container) return;

    // 只在用户位于底部附近时自动滚动，允许用户自由向上查看历史
    if (isUserNearBottomRef.current) {
      container.scrollTop = container.scrollHeight;
    }
  };

  // 检测是否有消息正在流式输出
  const isAnyMessageStreaming = messages.some(msg => msg.isStreaming);

  useEffect(() => {
    // 如果有消息正在流式输出，强制滚动到底部（跟随输出）
    // 否则使用智能滚动（只在用户在底部附近时滚动）
    scrollToBottom();
  }, [messages, isAnyMessageStreaming]);

  // 监听滚动事件，检测用户是否在底部附近
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      // 如果用户在距离底部 100px 以内，认为在底部
      isUserNearBottomRef.current = scrollHeight - scrollTop - clientHeight < 100;
    };

    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, []);

  // 当消息变化时保存会话
  useEffect(() => {
    if (!selectedKB || messages.length === 0) return;

    const now = new Date().toISOString();
    const sessionTitle =
      messages[0]?.content.slice(0, 30) + (messages[0]?.content.length > 30 ? '...' : '');

    setChatSessions(prevSessions => {
      let updatedSessions: ChatSession[];

      if (currentSessionId) {
        updatedSessions = prevSessions.map(session =>
          session.id === currentSessionId
            ? { ...session, messages, updatedAt: now }
            : session
        );
      } else {
        const newSession: ChatSession = {
          id: `session-${Date.now()}`,
          title: sessionTitle,
          messages,
          knowledgeBaseId: selectedKB.collection_id,
          knowledgeBaseName: formatKnowledgeBaseLabel(selectedKB),
          createdAt: now,
          updatedAt: now,
        };
        setCurrentSessionId(newSession.id);
        updatedSessions = [newSession, ...prevSessions];
      }

      const trimmedSessions = updatedSessions.slice(0, 50);
      localStorage.setItem('chat_sessions', JSON.stringify(trimmedSessions));
      return trimmedSessions;
    });
  }, [messages, selectedKB, currentSessionId]);

  // 中断生成
  const handleStopGeneration = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      // 注意：不在这里设置 isLoading 和更新消息，让 catch 块统一处理
    }
  };

  // 发送消息
  const handleSendMessage = async () => {
    if (!message.trim() || isLoading || !selectedKB) {
      if (!selectedKB) {
        toast.error('请先选择知识库');
      }
      return;
    }

    const userMessage: Message = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: message.trim(),
      timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
    };

    setMessages(prev => [...prev, userMessage]);
    setMessage('');
    setIsLoading(true);

    // 创建助手消息
    const assistantMessage: Message = {
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      content: '',
      timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      isStreaming: true,
      isThinking: enableThinking,  // 启用思考模式时显示"正在思考..."
    };
    setMessages(prev => [...prev, assistantMessage]);

    try {
      abortControllerRef.current = new AbortController();

      const response = await authFetch(`${config.chatApiUrl}/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          query: userMessage.content,
          collection_name: selectedKB.collection_id,
          llm_config: llmConfig,
          top_k: 10,
          score_threshold: 0.15,
          use_reranker: false,
          stream: true,
          return_source: true,
          history: messages.slice(-10).map(msg => ({
            role: msg.role,
            content: msg.content,
          })),
          socratic_mode: socraticMode,
          enable_thinking: enableThinking,
        }),
        signal: abortControllerRef.current.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) {
        throw new Error('无法读取响应流');
      }

      let accumulatedContent = '';
      let accumulatedReasoning = '';
      let sources: SourceDocument[] | undefined;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        const lines = chunk.split('\n').filter(line => line.trim());

        for (const line of lines) {
          try {
            const data = JSON.parse(line) as ChatStreamChunk;

            if (data.type === 'reasoning') {
              // 处理思考内容
              accumulatedReasoning += data.data || '';
              setMessages(prev =>
                prev.map(msg =>
                  msg.id === assistantMessage.id
                    ? { ...msg, reasoning: accumulatedReasoning, isThinking: false }
                    : msg
                )
              );
            } else if (data.type === 'content') {
              accumulatedContent += data.data;
              setMessages(prev =>
                prev.map(msg =>
                  msg.id === assistantMessage.id
                    ? { ...msg, content: accumulatedContent, isThinking: false }
                    : msg
                )
              );
            } else if (data.type === 'sources') {
              sources = data.data;
            } else if (data.type === 'metadata') {
              console.log('对话元数据:', data.data);
            } else if (data.type === 'error') {
              console.error('对话错误:', data.data);
              toast.error(`对话失败: ${data.data.error}`);
            }
          } catch (e) {
            console.warn('解析流数据失败:', line, e);
          }
        }
      }

      setMessages(prev =>
        prev.map(msg =>
          msg.id === assistantMessage.id
            ? { ...msg, isStreaming: false, isThinking: false, sources }
            : msg
        )
      );
    } catch (error: unknown) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        // 用户主动中断，保留已生成的内容，只更新流式状态
        console.log('用户中断生成');
        setMessages(prev =>
          prev.map(msg =>
            msg.id === assistantMessage.id
              ? { ...msg, isStreaming: false, isThinking: false }
              : msg
          )
        );
      } else {
        console.error('对话失败:', error);
        toast.error('对话失败，请稍后重试');
        // 只有真正的错误才删除消息
        setMessages(prev => prev.filter(msg => msg.id !== assistantMessage.id));
      }
    } finally {
      setIsLoading(false);
      abortControllerRef.current = null;
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  const handleNewChat = () => {
    setMessages([]);
    setCurrentSessionId(null);
    toast.success('已创建新对话');
  };

  const loadSession = (session: ChatSession) => {
    setMessages(session.messages);
    setCurrentSessionId(session.id);

    // 切换到对应的知识库
    const kb = knowledgeBases.find(k => k.collection_id === session.knowledgeBaseId);
    if (kb) {
      setSelectedKB(kb);
    }

    toast.success(`已加载对话: ${session.title}`);
  };

  const deleteSession = (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const updatedSessions = chatSessions.filter(s => s.id !== sessionId);
    setChatSessions(updatedSessions);
    localStorage.setItem('chat_sessions', JSON.stringify(updatedSessions));

    if (currentSessionId === sessionId) {
      setMessages([]);
      setCurrentSessionId(null);
    }

    toast.success('已删除对话');
  };

  const sessionListWidth = isHistoryOpen ? 280 : 56;

  return (
    <div className="flex h-full">
      {/* Left Sidebar */}
      <div
        className="glass-strong flex flex-col transition-[width] duration-300 ease-in-out overflow-hidden border-r border-[rgba(56,189,248,0.15)] relative"
        style={{ width: `${sessionListWidth}px` }}
      >
        {isHistoryOpen ? (
          <>
            <div className="p-4 border-b border-[rgba(56,189,248,0.15)] flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Sparkles size={18} className="text-[#38BDF8]" />
                <h3 className="text-[#E2E8F0]">对话历史</h3>
              </div>
              <div className="flex items-center gap-2">
                <motion.button
                  onClick={handleNewChat}
                  className="w-8 h-8 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                  aria-label="新建对话"
                >
                  <Plus size={18} className="text-[#38BDF8]" />
                </motion.button>
                <motion.button
                  onClick={onToggleHistory}
                  className="w-8 h-8 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                  aria-label="收起对话历史"
                >
                  <ChevronLeft size={18} className="text-[#38BDF8]" />
                </motion.button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-2 space-y-2">
              {chatSessions.length === 0 ? (
                <div className="text-[#94A3B8] text-sm text-center py-4">暂无历史对话</div>
              ) : (
                chatSessions.map(session => (
                  <motion.div
                    key={session.id}
                    onClick={() => loadSession(session)}
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
                          <p className="text-[#E2E8F0] text-sm font-medium truncate">
                            {session.title}
                          </p>
                        </div>
                        <p className="text-[#94A3B8] text-xs truncate">
                          {session.knowledgeBaseName} • {session.messages.length}条消息
                        </p>
                        <p className="text-[#64748b] text-xs mt-1">
                          {new Date(session.updatedAt).toLocaleString('zh-CN', {
                            month: 'numeric',
                            day: 'numeric',
                            hour: '2-digit',
                            minute: '2-digit'
                          })}
                        </p>
                      </div>
                      <motion.button
                        onClick={(e) => deleteSession(session.id, e)}
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
              onClick={handleNewChat}
              className="w-10 h-10 rounded-xl glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
              aria-label="新建对话"
            >
              <Plus size={20} className="text-[#38BDF8]" />
            </motion.button>
            <motion.button
              onClick={onToggleHistory}
              className="w-10 h-10 rounded-xl glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
              aria-label="展开对话历史"
            >
              <ChevronRight size={20} className="text-[#38BDF8]" />
            </motion.button>
          </div>
        )}

        <button
          type="button"
          onClick={() => onSetHistoryOpen(!isHistoryOpen)}
          aria-label={isHistoryOpen ? '收起对话历史' : '展开对话历史'}
          className="absolute right-0 top-0 h-full w-2 hover:bg-[rgba(56,189,248,0.12)] transition-colors"
        />
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col bg-transparent">
        {/* Top Bar */}
        <div className="h-16 glass border-b border-[rgba(56,189,248,0.15)] px-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            {/* 知识库选择 */}
            <Select
              value={selectedKB?.collection_id || '__empty__'}
              onValueChange={(value) => {
                if (value === '__empty__') return; // Ignore disabled placeholder
                const kb = knowledgeBases.find(k => k.collection_id === value);
                setSelectedKB(kb || null);
              }}
            >
              <SelectTrigger className="px-4 py-2 glass-strong border-[rgba(56,189,248,0.2)] rounded-xl text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.1)] transition-all focus:ring-2 focus:ring-[#38BDF8] h-auto">
                <SelectValue placeholder="暂无知识库" />
              </SelectTrigger>
              <SelectContent className="bg-[rgba(15,23,36,0.98)] border-[rgba(56,189,248,0.2)] text-[#CBD5E1]">
                {knowledgeBases.length === 0 && (
                  <SelectItem value="__empty__" disabled>暂无知识库</SelectItem>
                )}
                {knowledgeBases.map(kb => (
                  <SelectItem key={kb.collection_id} value={kb.collection_id}>
                    {formatKnowledgeBaseLabel(kb)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* 模型选择 */}
            <Select
              value={llmConfig.model_name}
              onValueChange={(value) => {
                const selectedModel = availableModels.find(m => m.name === value);
                if (selectedModel) {
                  // 根据provider动态切换API配置
                  if (selectedModel.provider === 'deepseek' && deepseekConfig) {
                    setLLMConfig({
                      ...llmConfig,
                      model_name: selectedModel.name,
                      api_url: selectedModel.api_url || deepseekConfig.api_url,
                      api_key: deepseekConfig.api_key,
                    });
                  } else {
                    // 切换到 DashScope 模型时，需要恢复 DashScope 的 API key
                    setLLMConfig({
                      ...llmConfig,
                      model_name: selectedModel.name,
                      api_url: selectedModel.api_url || dashscopeConfig?.api_url || 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                      api_key: dashscopeConfig?.api_key || '',
                    });
                  }
                  // 如果切换到不支持思考的模型，关闭思考模式
                  if (!selectedModel.supports_thinking) {
                    setEnableThinking(false);
                  }
                }
              }}
            >
              <SelectTrigger className="px-4 py-2 glass-strong border-[rgba(56,189,248,0.2)] rounded-xl text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.1)] transition-all focus:ring-2 focus:ring-[#38BDF8] text-sm h-auto">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[rgba(15,23,36,0.98)] border-[rgba(56,189,248,0.2)] text-[#CBD5E1]">
                {availableModels.map(model => (
                  <SelectItem key={model.name} value={model.name}>
                    {model.display}{model.provider === 'deepseek' ? ' (DeepSeek)' : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* 思考模式开关 */}
            {(() => {
              const selectedModel = availableModels.find(m => m.name === llmConfig.model_name);
              const canEnableThinking = selectedModel?.supports_thinking ?? false;
              return canEnableThinking ? (
                <label className="flex items-center gap-2 text-sm text-[#94A3B8] cursor-pointer whitespace-nowrap">
                  <input
                    type="checkbox"
                    checked={enableThinking}
                    onChange={(e) => setEnableThinking(e.target.checked)}
                    className="accent-[#38BDF8]"
                  />
                  <span>思考模式</span>
                </label>
              ) : null;
            })()}
          </div>
          <div className="flex items-center gap-4">
            <div className="text-[#94A3B8] text-sm">
              {messages.length > 0 && `${messages.length} 条消息`}
            </div>
          </div>
        </div>

        {/* Messages Container */}
        <div ref={messagesContainerRef} className="flex-1 overflow-y-auto overscroll-contain">
          <div className={`flex flex-col min-h-full p-6 ${messages.length === 0 ? 'justify-center' : 'justify-end space-y-6'}`}>
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-center">
              <div className="w-24 h-24 rounded-full bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center mb-6 animate-pulse-glow">
                <Bot size={48} className="text-[#0F172A]" />
              </div>
              <h2 className="text-2xl text-gradient mb-3">开始新对话</h2>
              <p className="text-[#94A3B8] max-w-md">
                {selectedKB ? `已选择知识库「${formatKnowledgeBaseLabel(selectedKB)}」，现在可以向我提问了` : '请先选择一个知识库，然后开始对话'}
              </p>
            </div>
          ) : (
            <AnimatePresence>
              {messages.map((msg, index) => (
                <motion.div
                  key={msg.id}
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, delay: index * 0.05 }}
                >
                  {msg.role === 'assistant' ? (
                    <div className="flex gap-3 items-start">
                      <motion.div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-[#38BDF8] to-[#0EA5E9] flex items-center justify-center flex-shrink-0 shadow-lg">
                        <Bot size={22} className="text-[#0F1724]" />
                      </motion.div>
                      <div className="flex-1 max-w-[90%]">
                        <motion.div className="bg-transparent pl-2 py-2">
                          {/* 思考过程显示 */}
                          {(msg.reasoning || msg.isThinking) && (
                            <details className="mb-3 text-xs text-[#94A3B8]" open={msg.isThinking}>
                              <summary className="cursor-pointer flex items-center gap-2">
                                <Brain size={12} className={msg.isThinking ? 'animate-pulse' : ''} />
                                {msg.isThinking ? '正在思考...' : '思考过程'}
                              </summary>
                              {msg.reasoning ? (
                                <div className="mt-2 break-words">{msg.reasoning}</div>
                              ) : (
                                <div className="mt-2 text-[#64748B] italic">思考内容加载中...</div>
                              )}
                            </details>
                          )}
                          <div className="prose prose-invert max-w-none text-[#CBD5E1]">
                            <ReactMarkdown
                              remarkPlugins={markdownRemarkPlugins}
                              rehypePlugins={markdownRehypePlugins as any}
                              components={{
                                ...markdownTableComponents,
                                h1: ({ node, ...props }) => {
                                  void node;
                                  return <h1 className="text-xl text-gradient mb-3 font-bold" {...props} />;
                                },
                                h2: ({ node, ...props }) => {
                                  void node;
                                  return <h2 className="text-lg text-[#38BDF8] mb-2 font-semibold" {...props} />;
                                },
                                h3: ({ node, ...props }) => {
                                  void node;
                                  return <h3 className="text-base text-[#38BDF8] mb-2 font-medium" {...props} />;
                                },
                                p: ({ node, ...props }) => {
                                  void node;
                                  return <p className="text-[#CBD5E1] mb-2 leading-relaxed" {...props} />;
                                },
                                ul: ({ node, ...props }) => {
                                  void node;
                                  return (
                                    <ul className="list-disc list-inside space-y-1 text-[#CBD5E1] mb-2" {...props} />
                                  );
                                },
                                ol: ({ node, ...props }) => {
                                  void node;
                                  return (
                                    <ol className="list-decimal list-inside space-y-1 text-[#CBD5E1] mb-2" {...props} />
                                  );
                                },
                                li: ({ node, ...props }) => {
                                  void node;
                                  return <li className="text-[#CBD5E1]" {...props} />;
                                },
                                strong: ({ node, ...props }) => {
                                  void node;
                                  return <strong className="text-[#38BDF8] font-semibold" {...props} />;
                                },
                                em: ({ node, ...props }) => {
                                  void node;
                                  return <em className="text-[#34D399] italic" {...props} />;
                                },
                                code: ({ node, ...props }) => {
                                  void node;
                                  return (
                                    <code
                                      className="bg-[#1E293B] text-[#34D399] px-1.5 py-0.5 rounded text-sm border border-slate-700"
                                      {...props}
                                    />
                                  );
                                },
                                pre: ({ node, ...props }) => {
                                  void node;
                                  return (
                                    <pre className="bg-[#0F1724] p-4 rounded-xl overflow-x-auto my-4 border border-slate-800 shadow-inner" {...props} />
                                  );
                                },
                                img: ({ node, ...props }) => {
                                  void node;
                                  let src = props.src || '';

                                  // 防御：如果 LLM 输出了绝对 HTTP URL（含 /document/ 路径），提取相对路径
                                  const docMatch = src.match(/https?:\/\/[^/]+?(\/document\/.*)/);
                                  if (docMatch) {
                                    src = docMatch[1];
                                  }

                                  // 处理以 /document/ 开头的相对路径，转换为通过 Nginx 代理的 URL
                                  if (src.startsWith('/document/')) {
                                    src = `${config.milvusApiUrl}${src}`;
                                  }

                                  // 添加认证token
                                  src = appendAuthTokenToUrl(src);

                                  return (
                                    <img
                                      className="max-w-full h-auto rounded-xl border border-[rgba(56,189,248,0.2)] my-4 shadow-md"
                                      {...props}
                                      src={src}
                                    />
                                  );
                                },
                                blockquote: ({ node, ...props }) => {
                                  void node;
                                  return (
                                    <blockquote
                                      className="border-l-4 border-[#38BDF8] pl-4 py-2 my-2 text-[#94A3B8] italic bg-[#1E293B]/30 rounded-r-lg"
                                      {...props}
                                    />
                                  );
                                },
                                hr: ({ node, ...props }) => {
                                  void node;
                                  return <hr className="my-4 border-t border-[rgba(56,189,248,0.3)]" {...props} />;
                                },
                              }}
                                >
                                  {normalizeMathDelimiters(msg.content)}
                            </ReactMarkdown>
                            {msg.isStreaming && <span className="inline-block w-2 h-5 bg-[#38BDF8] ml-1 animate-pulse" />}
                          </div>
                        </motion.div>
                        {msg.sources && msg.sources.length > 0 && (
                          <div className="mt-3">
                            <motion.button
                              onClick={() => setExpandedCitation(expandedCitation === msg.id ? null : msg.id)}
                              className="text-[#38BDF8] text-sm flex items-center gap-1 px-3 py-1.5 glass rounded-lg border border-[rgba(56,189,248,0.2)]"
                            >
                              📚 引用来源 [{msg.sources.length}个]
                              <ChevronDown size={14} className={`transition-transform ${expandedCitation === msg.id ? 'rotate-180' : ''}`} />
                            </motion.button>
                            {expandedCitation === msg.id && (
                              <div className="mt-3 space-y-2">
                                {msg.sources.map((source, idx) => (
                                  <div key={idx} className="glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl p-4 text-sm">
                                    <div className="flex items-center gap-2 mb-2">
                                      <span className="text-[#CBD5E1]">📄 {source.filename}</span>
                                    </div>
                                    <div className="flex gap-2 mb-3">
                                      <span className="px-2 py-1 bg-[rgba(56,189,248,0.1)] text-[#38BDF8] rounded-lg text-xs">
                                        相似度: {source.score.toFixed(3)}
                                      </span>
                                    </div>
                                    <div className="text-[#94A3B8] text-xs line-clamp-3">{source.chunk_text}</div>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                        <div className="text-[#94A3B8] text-xs mt-2">{msg.timestamp}</div>
                      </div>
                    </div>
                  ) : (
                    <div className="flex gap-3 items-start justify-end">
                      <div className="max-w-[70%]">
                        <div className="bg-[rgba(56,189,248,0.15)] border border-[rgba(56,189,248,0.3)] text-[#E2E8F0] rounded-2xl p-5 shadow-lg backdrop-blur-sm">
                          <p className="whitespace-pre-wrap">{msg.content}</p>
                        </div>
                        <div className="text-[#94A3B8] text-xs mt-2 text-right">{msg.timestamp}</div>
                      </div>
                      <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-[#8b5cf6] to-[#6366f1] flex items-center justify-center flex-shrink-0 shadow-lg">
                        <User size={22} className="text-white" />
                      </div>
                    </div>
                  )}
                </motion.div>
              ))}
            </AnimatePresence>
          )}
          <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Input Area */}
        <div className="glass-strong border-t border-[rgba(56,189,248,0.15)] p-4">
          {/* 苏格拉底模式按钮 - 放在输入框左上方 */}
          <div className="mb-3 flex items-center gap-2">
            <motion.button
              onClick={() => setSocraticMode(!socraticMode)}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                socraticMode
                  ? 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-white shadow-[0_0_15px_rgba(56,189,248,0.4)]'
                  : 'glass text-[#94A3B8] hover:text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.1)] border border-[rgba(56,189,248,0.3)]'
              }`}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
              title="启用苏格拉底问答法：AI将通过提问引导你理解知识点"
            >
              <GraduationCap size={16} />
              <span>苏格拉底问答法</span>
              {socraticMode && <span className="w-2 h-2 rounded-full bg-white animate-pulse" />}
            </motion.button>
            {socraticMode && (
              <span className="text-xs text-[#94A3B8]">
                已启用：AI将通过苏格拉底式提问引导你思考
              </span>
            )}
          </div>

          {/* 输入框和发送按钮 */}
          <div className="flex gap-3 items-center">
            <div className="flex-1 relative">
              <textarea
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                onKeyPress={handleKeyPress}
                placeholder={
                  socraticMode
                    ? '🧠 提出你的问题，AI将引导你思考...'
                    : (selectedKB ? '💬 输入你的问题...' : '⚠️ 请先选择知识库')
                }
                disabled={!selectedKB || isLoading}
                className="w-full min-h-[56px] max-h-[200px] px-4 py-3 glass-strong border border-[rgba(56,189,248,0.2)] rounded-xl focus:outline-none focus:ring-2 focus:ring-[#38BDF8] resize-none text-[#CBD5E1] placeholder-[#94A3B8] transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                rows={1}
              />
              <div className="absolute bottom-3 right-3 text-xs text-[#94A3B8]">
                {message.length}/2000
              </div>
            </div>
            <motion.button
              onClick={isLoading ? handleStopGeneration : handleSendMessage}
              disabled={!isLoading && (!message.trim() || !selectedKB)}
              className={`w-14 h-14 rounded-xl flex items-center justify-center transition-all disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0 ${
                isLoading
                  ? 'bg-gradient-to-r from-[#EF4444] to-[#DC2626] text-white'
                  : 'bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A]'
              }`}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
              title={isLoading ? '停止生成' : '发送消息'}
            >
              {isLoading ? <Square size={20} fill="currentColor" /> : <Send size={22} />}
            </motion.button>
          </div>
        </div>
      </div>
    </div>
  );
}
