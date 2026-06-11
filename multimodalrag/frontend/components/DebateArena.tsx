import { useEffect, useState, useRef } from 'react';
import { motion } from 'motion/react';
import { Sparkles, Plus, Trash2, MessageSquare, Flag, Scale, Brain, ChevronLeft, ChevronRight } from 'lucide-react';
import { toast } from 'sonner';
import ReactMarkdown from 'react-markdown';
import { ModeId } from '../src/modes';
import { config } from '../src/config';
import { authFetch } from '../src/api/auth';
import { markdownRemarkPlugins, markdownRehypePlugins, normalizeMathDelimiters } from './markdownPlugins';
import { markdownTableComponents } from './markdownTableComponents';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface DebateJudgeDecision {
  finished: boolean;
  notes?: string | null;
  suggested_answer?: string | null;
}

interface DebateRound {
  round_id: number;
  label: string;
  affirmative_speech: string;
  opposition_speech: string;
  judge: DebateJudgeDecision;
}

interface DebateRunResponse {
  topic: string;
  max_rounds: number;
  total_rounds_run: number;
  finished_early: boolean;
  model_name: string;
  rounds: DebateRound[];
}

interface ModelOption {
  name: string;
  display: string;
  provider: string;
}

interface ChatConfigResponse {
  status: 'success' | 'error';
  config?: {
    llm?: {
      model_name?: string;
    };
    available_models: ModelOption[];
  };
}

interface DebateSession {
  id: string;
  motion: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  mode: ModeId;
  modelName?: string;
  rounds?: DebateRound[];
}

type DebateStreamEvent =
  | {
      type: 'content';
      side: 'affirmative' | 'opposition' | 'moderator';
      round_id: number;
      full: string;
    }
  | {
      type: 'judge';
      round_id: number;
      decision: DebateJudgeDecision;
    }
  | {
      type: 'end';
      total_rounds: number;
    };

interface DebateArenaProps {
  mode: ModeId;
  isHistoryOpen: boolean;
  onToggleHistory: () => void;
  onSetHistoryOpen: (isOpen: boolean) => void;
}

const isDebateSession = (session: unknown): session is DebateSession => {
  if (!session || typeof session !== 'object') return false;
  const candidate = session as Partial<DebateSession>;
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.motion === 'string' &&
    typeof candidate.title === 'string' &&
    typeof candidate.createdAt === 'string' &&
    typeof candidate.updatedAt === 'string' &&
    typeof candidate.mode === 'string'
  );
};

const DEBATE_SESSION_STORAGE_KEY = 'debate_sessions';

const ROUND_LABELS: Record<number, { title: string; subtitle: string }> = {
  1: { title: '开篇立论', subtitle: 'Opening / Main Case' },
  2: { title: '攻辩与扩展', subtitle: 'Rebuttals & Additional Arguments' },
  3: { title: '比较与评估', subtitle: 'Evaluation / Weighing' },
};

const splitModeratorOutput = (text: string) => {
  const trimmed = text.trim();
  if (!trimmed) {
    return { evaluation: '', judgement: '' };
  }

  const markers = ['最终裁决', 'Final Judgment', 'Final Answer', '最终答案', '正确答案'];
  const marker = markers.find(candidate => trimmed.includes(candidate));
  if (!marker) {
    return { evaluation: trimmed, judgement: '' };
  }

  const markerIndex = trimmed.lastIndexOf(marker);
  const rawEvaluation = trimmed.slice(0, markerIndex).trim();
  const evaluation = rawEvaluation
    .replace(/^综合评议[:：\s-]+/i, '')
    .replace(/^Evaluation[:：\s-]+/i, '')
    .trim();
  const after = trimmed.slice(markerIndex + marker.length);
  const judgement = after.replace(/^[:：\s-]+/, '').trim();

  return { evaluation, judgement };
};

// Markdown renderer component for debate content
const DebateMarkdown = ({ content, side }: { content: string; side: 'affirmative' | 'opposition' | 'judge' }) => {
  if (!content) return null;

  const borderColor = side === 'affirmative'
    ? 'border-[rgba(34,197,94,0.3)]'
    : side === 'opposition'
      ? 'border-[rgba(129,140,248,0.3)]'
      : 'border-[rgba(251,191,36,0.3)]';

  return (
    <ReactMarkdown
      remarkPlugins={markdownRemarkPlugins}
      rehypePlugins={markdownRehypePlugins as any}
      components={{
        ...markdownTableComponents,
        p: ({ children }) => <p className="mb-3 leading-relaxed text-[#E5E7EB]">{children}</p>,
        h1: ({ children }) => <h1 className="text-lg font-bold mb-3 mt-4 text-[#E2E8F0]">{children}</h1>,
        h2: ({ children }) => <h2 className="text-base font-semibold mb-2 mt-3 text-[#E2E8F0]">{children}</h2>,
        h3: ({ children }) => <h3 className="text-sm font-medium mb-2 mt-2 text-[#cbd5f5]">{children}</h3>,
        ul: ({ children }) => <ul className="list-disc list-inside mb-3 space-y-1 text-[#E5E7EB]">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal list-inside mb-3 space-y-1 text-[#E5E7EB]">{children}</ol>,
        li: ({ children }) => <li className="text-[#E5E7EB]">{children}</li>,
        strong: ({ children }) => <strong className="font-semibold text-[#F0F4FF]">{children}</strong>,
        em: ({ children }) => <em className="italic text-[#94A3B8]">{children}</em>,
        blockquote: ({ children }) => (
          <blockquote className={`border-l-2 ${borderColor} pl-3 my-3 italic text-[#94A3B8]`}>
            {children}
          </blockquote>
        ),
        code: ({ children, className }) => {
          const isInline = !className;
          if (isInline) {
            return (
              <code className="bg-[rgba(0,0,0,0.3)] px-1.5 py-0.5 rounded text-sm font-mono text-[#38BDF8]">
                {children}
              </code>
            );
          }
          return (
            <code className={`block bg-[rgba(0,0,0,0.3)] p-3 rounded-lg text-sm font-mono text-[#E5e7eb] overflow-x-auto ${className}`}>
              {children}
            </code>
          );
        },
        pre: ({ children }) => <pre className="mb-3">{children}</pre>,
        hr: () => <hr className="my-4 border-[rgba(148,163,184,0.2)]" />,
      }}
    >
      {normalizeMathDelimiters(content)}
    </ReactMarkdown>
  );
};

export function DebateArena({ mode, isHistoryOpen, onToggleHistory, onSetHistoryOpen }: DebateArenaProps) {
  const [sessions, setSessions] = useState<DebateSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [motionInput, setMotionInput] = useState('');
  const [activeRound, setActiveRound] = useState<number>(1);
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('deepseek-v4-flash');
  const [isRunningDebate, setIsRunningDebate] = useState(false);
  const [streamAffirmative, setStreamAffirmative] = useState<Record<number, string>>({});
  const [streamOpposition, setStreamOpposition] = useState<Record<number, string>>({});
  const [streamJudges, setStreamJudges] = useState<Record<number, DebateJudgeDecision>>({});
  const [streamModerator, setStreamModerator] = useState<Record<number, string>>({});

  // Refs for auto-scrolling
  const mainStageRef = useRef<HTMLDivElement>(null);
  const isUserNearBottomRef = useRef(true);

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
    const init = async () => {
      try {
        const raw = localStorage.getItem(DEBATE_SESSION_STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw) as unknown;
          if (Array.isArray(parsed)) {
            const validSessions = parsed.filter(isDebateSession);
            validSessions.sort(
              (a, b) =>
                new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
            );
            setSessions(validSessions);
            if (validSessions.length > 0) {
              const first = validSessions[0];
              setCurrentSessionId(first.id);
              setMotionInput(first.motion);
              if (first.modelName) {
                setSelectedModel(first.modelName);
              }
            }
          }
        }
      } catch (error) {
        console.error('加载辩论历史失败:', error);
      }

      // 从对话服务获取可用模型列表，沿用 Chat 配置
      try {
        const res = await authFetch(`${config.chatApiUrl}/config/default`);
        const data = (await res.json()) as ChatConfigResponse;
        if (data.status === 'success' && data.config) {
          setAvailableModels(data.config.available_models || []);
          const defaultModel =
            data.config.llm?.model_name && data.config.llm.model_name.trim()
              ? data.config.llm.model_name.trim()
              : 'deepseek-v4-flash';
          setSelectedModel(prev => (prev ? prev : defaultModel));
        }
      } catch (error) {
        console.error('加载辩论模型配置失败:', error);
      }
    };

    void init();
  }, []);

  // Auto-scroll logic
  const scrollToBottom = () => {
    const container = mainStageRef.current;
    if (!container) return;
    // 只在用户位于底部附近时自动滚动，允许用户自由向上查看历史
    if (isUserNearBottomRef.current) {
      container.scrollTo({
        top: container.scrollHeight,
        behavior: 'smooth'
      });
    }
  };

  // Check if streaming is active
  const isStreaming = isRunningDebate && (
    !!streamAffirmative[activeRound] ||
    !!streamOpposition[activeRound] ||
    !!streamModerator[activeRound]
  );

  // Auto-scroll when streaming content updates
  useEffect(() => {
    scrollToBottom();
  }, [streamAffirmative, streamOpposition, streamModerator, streamJudges, isStreaming]);

  // Monitor scroll position to detect if user is near bottom
  useEffect(() => {
    const container = mainStageRef.current;
    if (!container) return;
    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      isUserNearBottomRef.current = scrollHeight - scrollTop - clientHeight < 150;
    };
    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, []);

  const persistSessions = (next: DebateSession[]) => {
    setSessions(next);
    localStorage.setItem(DEBATE_SESSION_STORAGE_KEY, JSON.stringify(next));
  };

  const handleNewDebate = () => {
    setMotionInput('');
    setCurrentSessionId(null);
    setActiveRound(1);
    setStreamAffirmative({});
    setStreamOpposition({});
    setStreamJudges({});
    setStreamModerator({});
    toast.success('已创建新辩论');
  };

  const handleSaveMotion = () => {
    const trimmed = motionInput.trim();
    if (!trimmed) {
      toast.error('请先输入辩题');
      return;
    }

    const now = new Date().toISOString();
    const title =
      trimmed.slice(0, 30) + (trimmed.length > 30 ? '...' : '');

    let nextSessions: DebateSession[];

    if (currentSessionId) {
      nextSessions = sessions.map(session =>
        session.id === currentSessionId
          ? {
              ...session,
              motion: trimmed,
              title,
              updatedAt: now,
              modelName: selectedModel,
            }
          : session,
      );
    } else {
      const newSession: DebateSession = {
        id: `debate-session-${Date.now()}`,
        motion: trimmed,
        title,
        createdAt: now,
        updatedAt: now,
        mode,
        modelName: selectedModel,
      };
      nextSessions = [newSession, ...sessions];
      setCurrentSessionId(newSession.id);
    }

    // 限制本地存储数量，避免过多历史
    const limited = nextSessions.slice(0, 50);
    persistSessions(limited);
    toast.success('已保存辩题');
  };

  const handleLoadSession = (session: DebateSession) => {
    setCurrentSessionId(session.id);
    setMotionInput(session.motion);
    setActiveRound(1);
    setStreamAffirmative({});
    setStreamOpposition({});
    setStreamJudges({});
    setStreamModerator({});
    if (session.modelName) {
      setSelectedModel(session.modelName);
    }
    toast.success(`已加载辩论：${session.title}`);
  };

  const handleDeleteSession = (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const filtered = sessions.filter(s => s.id !== sessionId);
    persistSessions(filtered);

    if (currentSessionId === sessionId) {
      setCurrentSessionId(null);
      setMotionInput('');
      setActiveRound(1);
      setStreamAffirmative({});
      setStreamOpposition({});
      setStreamJudges({});
      setStreamModerator({});
    }

    toast.success('已删除辩论');
  };

  const currentSession = sessions.find(s => s.id === currentSessionId) || null;
  const currentRoundsCount = currentSession?.rounds?.length ?? 0;
  const streamModeratorText = streamModerator[3] ?? '';
  const streamModeratorParts = splitModeratorOutput(streamModeratorText);
  const finalJudgeDecision = isRunningDebate
    ? streamJudges[3] ?? null
    : currentSession?.rounds?.find(r => r.round_id === 3)?.judge ?? null;
  const finalJudgeNotes = isRunningDebate
    ? streamModeratorParts.evaluation || finalJudgeDecision?.notes || ''
    : finalJudgeDecision?.notes ?? '';
  const finalJudgeAnswer = isRunningDebate
    ? streamModeratorParts.judgement || finalJudgeDecision?.suggested_answer || ''
    : finalJudgeDecision?.suggested_answer ?? '';
  const hasFinalJudgeDecision = isRunningDebate
    ? Boolean(streamModeratorText || finalJudgeDecision)
    : Boolean(finalJudgeDecision);

  const findCurrentRound = (): DebateRound | null => {
    if (!currentSession || !currentSession.rounds || currentSession.rounds.length === 0) {
      return null;
    }
    const round = currentSession.rounds.find(r => r.round_id === activeRound);
    return round || null;
  };

  const handleRunDebate = async () => {
    const trimmed = motionInput.trim();
    if (!trimmed) {
      toast.error('请先输入辩题');
      return;
    }
    if (!selectedModel) {
      toast.error('模型配置尚未加载，请稍后重试');
      return;
    }

    setIsRunningDebate(true);
    setStreamAffirmative({});
    setStreamOpposition({});
    setStreamJudges({});
    setStreamModerator({});
    try {
      const body = {
        topic: trimmed,
        model_name: selectedModel,
      };

      const response = await authFetch(`${config.debateApiUrl}/debates/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });

      if (!response.ok || !response.body) {
        const errorText = await response.text().catch(() => '');
        console.error('运行辩论失败:', errorText);
        toast.error('运行辩论失败，请稍后重试');
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      const fullAffirmative: Record<number, string> = {};
      const fullOpposition: Record<number, string> = {};
      const fullJudges: Record<number, DebateJudgeDecision> = {};
      let hasContentEvents = false;
      let buffer = '';

      const processStreamLine = (line: string) => {
        const trimmedLine = line.trim();
        if (!trimmedLine) return;

        try {
          console.debug('辩论流原始行:', trimmedLine);
          const data = JSON.parse(trimmedLine) as DebateStreamEvent;
          console.debug('辩论流事件:', data);

          if (data.type === 'content') {
            if (data.side === 'affirmative') {
              hasContentEvents = true;
              fullAffirmative[data.round_id] = data.full;
              setStreamAffirmative(prev => ({
                ...prev,
                [data.round_id]: data.full,
              }));
            } else if (data.side === 'opposition') {
              hasContentEvents = true;
              fullOpposition[data.round_id] = data.full;
              setStreamOpposition(prev => ({
                ...prev,
                [data.round_id]: data.full,
              }));
            } else if (data.side === 'moderator') {
              setStreamModerator(prev => ({
                ...prev,
                [data.round_id]: data.full,
              }));
            }
          } else if (data.type === 'judge') {
            fullJudges[data.round_id] = data.decision;
            setStreamJudges(prev => ({
              ...prev,
              [data.round_id]: data.decision,
            }));
          } else if (data.type === 'end') {
            const now = new Date().toISOString();
            const title =
              trimmed.slice(0, 30) + (trimmed.length > 30 ? '...' : '');

            setSessions(prevSessions => {
              const rounds: DebateRound[] = [];
              for (let r = 1; r <= data.total_rounds; r += 1) {
                const judgeDecision = fullJudges[r] || {
                  finished: r === data.total_rounds,
                  notes: null,
                  suggested_answer: null,
                };
                rounds.push({
                  round_id: r,
                  label: ROUND_LABELS[r].subtitle,
                  affirmative_speech: fullAffirmative[r] || '',
                  opposition_speech: fullOpposition[r] || '',
                  judge: judgeDecision,
                });
              }

              let updated: DebateSession[];
              if (currentSessionId) {
                updated = prevSessions.map(session =>
                  session.id === currentSessionId
                    ? {
                        ...session,
                        motion: trimmed,
                        title,
                        updatedAt: now,
                        modelName: selectedModel,
                        rounds,
                      }
                    : session,
                );
              } else {
                const newSession: DebateSession = {
                  id: `debate-session-${Date.now()}`,
                  motion: trimmed,
                  title,
                  createdAt: now,
                  updatedAt: now,
                  mode,
                  modelName: selectedModel,
                  rounds,
                };
                updated = [newSession, ...prevSessions];
                setCurrentSessionId(newSession.id);
              }

              const limited = updated.slice(0, 50);
              localStorage.setItem(
                DEBATE_SESSION_STORAGE_KEY,
                JSON.stringify(limited),
              );
              return limited;
            });
          }
        } catch (err) {
          console.warn('解析辩论流数据失败:', trimmedLine, err);
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          processStreamLine(line);
        }
      }

      buffer += decoder.decode();
      if (buffer.trim()) {
        processStreamLine(buffer);
      }

      // 如果流式事件中没有任何 content，回退到非流式接口以确保至少显示最终结果
      if (!hasContentEvents) {
        console.warn('流式辩论未收到内容事件，回退到 /debates/run 获取完整结果');
        try {
          const fallbackResponse = await authFetch(
            `${config.debateApiUrl}/debates/run`,
            {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(body),
            },
          );
          if (fallbackResponse.ok) {
            const result = (await fallbackResponse.json()) as DebateRunResponse;
            const now = new Date().toISOString();
            const title =
              trimmed.slice(0, 30) + (trimmed.length > 30 ? '...' : '');

            setSessions(prevSessions => {
              let updated: DebateSession[];

              if (currentSessionId) {
                updated = prevSessions.map(session =>
                  session.id === currentSessionId
                    ? {
                        ...session,
                        motion: trimmed,
                        title,
                        updatedAt: now,
                        modelName: result.model_name,
                        rounds: result.rounds,
                      }
                    : session,
                );
              } else {
                const newSession: DebateSession = {
                  id: `debate-session-${Date.now()}`,
                  motion: trimmed,
                  title,
                  createdAt: now,
                  updatedAt: now,
                  mode,
                  modelName: result.model_name,
                  rounds: result.rounds,
                };
                updated = [newSession, ...prevSessions];
                setCurrentSessionId(newSession.id);
              }

              const limited = updated.slice(0, 50);
              localStorage.setItem(
                DEBATE_SESSION_STORAGE_KEY,
                JSON.stringify(limited),
              );
              return limited;
            });
          } else {
            const txt = await fallbackResponse.text().catch(() => '');
            console.error('/debates/run 回退请求失败:', txt);
          }
        } catch (fbErr) {
          console.error('回退到 /debates/run 时出错:', fbErr);
        }
      }

      setActiveRound(1);
      toast.success('辩论已完成，可在下方查看每一轮的正反发言');
    } catch (error) {
      console.error('运行辩论失败:', error);
      toast.error('运行辩论失败，请稍后重试');
    } finally {
      setIsRunningDebate(false);
    }
  };

  const sessionListWidth = isHistoryOpen ? 280 : 56;

  return (
    <div className="flex h-[calc(100vh-64px)]">
      {/* Left: Debate History */}
      <div
        className="glass-strong border-r border-[rgba(56,189,248,0.15)] flex flex-col transition-[width] duration-300 ease-in-out overflow-hidden relative"
        style={{ width: `${sessionListWidth}px` }}
      >
        {isHistoryOpen ? (
          <>
            <div className="p-4 border-b border-[rgba(56,189,248,0.15)] flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Sparkles size={18} className="text-[#38BDF8]" />
                <h3 className="text-[#E2E8F0]">辩论历史</h3>
              </div>
              <div className="flex items-center gap-2">
                <motion.button
                  onClick={handleNewDebate}
                  className="w-8 h-8 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                  aria-label="新建辩论"
                >
                  <Plus size={18} className="text-[#38BDF8]" />
                </motion.button>
                <motion.button
                  onClick={onToggleHistory}
                  className="w-8 h-8 rounded-lg glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                  aria-label="收起辩论历史"
                >
                  <ChevronLeft size={18} className="text-[#38BDF8]" />
                </motion.button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-2 space-y-2">
              {sessions.length === 0 ? (
                <div className="text-[#94A3B8] text-sm text-center py-4">
                  暂无辩论记录
                </div>
              ) : (
                sessions.map(session => (
                  <motion.div
                    key={session.id}
                    onClick={() => handleLoadSession(session)}
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
                          <MessageSquare
                            size={14}
                            className="text-[#38BDF8] flex-shrink-0"
                          />
                          <p className="text-[#E2E8F0] text-sm font-medium truncate">
                            {session.title}
                          </p>
                        </div>
                        <p className="text-[#94A3B8] text-xs truncate">
                          {new Date(session.updatedAt).toLocaleString('zh-CN', {
                            month: 'numeric',
                            day: 'numeric',
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                        </p>
                      </div>
                      <motion.button
                        onClick={e => handleDeleteSession(session.id, e)}
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
              onClick={handleNewDebate}
              className="w-10 h-10 rounded-xl glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
              aria-label="新建辩论"
            >
              <Plus size={20} className="text-[#38BDF8]" />
            </motion.button>
            <motion.button
              onClick={onToggleHistory}
              className="w-10 h-10 rounded-xl glass hover:bg-[rgba(56,189,248,0.1)] flex items-center justify-center transition-all"
              whileHover={{ scale: 1.1 }}
              whileTap={{ scale: 0.9 }}
              aria-label="展开辩论历史"
            >
              <ChevronRight size={20} className="text-[#38BDF8]" />
            </motion.button>
          </div>
        )}

        <button
          type="button"
          onClick={() => onSetHistoryOpen(!isHistoryOpen)}
          aria-label={isHistoryOpen ? '收起辩论历史' : '展开辩论历史'}
          className="absolute right-0 top-0 h-full w-2 hover:bg-[rgba(56,189,248,0.12)] transition-colors"
        />
      </div>

      {/* Right: Debate Stage */}
      <div className="flex-1 flex flex-col bg-[rgba(15,23,42,0.3)]">
        {/* Top Bar: Motion & Rounds */}
        <div className="h-20 glass border-b border-[rgba(56,189,248,0.15)] px-6 flex items-center justify-between">
          <div className="flex items-center gap-4 max-w-[60%]">
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-2">
                <Flag size={18} className="text-[#38BDF8]" />
                <span className="text-sm text-[#94A3B8]">当前辩题</span>
              </div>
              {motionInput.trim() ? (
                <p className="text-[#E2E8F0] text-sm truncate">
                  「{motionInput.trim()}」
                </p>
              ) : (
                <p className="text-[#64748B] text-sm">
                  请输入辩题，开启一场多智能体的头脑风暴辩论
                </p>
              )}
            </div>
            {/* 模型选择 - 移到辩题旁边 */}
            <Select value={selectedModel} onValueChange={setSelectedModel}>
              <SelectTrigger className="pl-3 pr-8 py-1.5 text-xs rounded-full glass-strong border-[rgba(148,163,184,0.6)] text-[#E2E8F0] bg-[rgba(15,23,42,0.8)] hover:border-[#38BDF8] focus:ring-1 focus:ring-[#38BDF8] h-8 w-auto">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[rgba(15,23,36,0.98)] border-[rgba(56,189,248,0.2)] text-[#CBD5E1]">
                {availableModels.length === 0 && (
                  <SelectItem value="deepseek-v4-flash">deepseek-v4-flash</SelectItem>
                )}
                {availableModels.map(model => (
                  <SelectItem key={model.name} value={model.name}>
                    {model.display || model.name}{model.provider === 'deepseek' ? ' (DeepSeek)' : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="flex items-center gap-2 text-xs text-[#94A3B8]">
              <Brain size={14} className="text-[#34D399]" />
              <span>三轮结构化辩论 · 聚焦思路发散</span>
            </div>
            <div className="flex items-center gap-3">
              {([1, 2, 3] as const).map(round => {
                const label = ROUND_LABELS[round];
                const isActive = activeRound === round;
                return (
                  <button
                    key={round}
                    type="button"
                    onClick={() => setActiveRound(round)}
                    className={`flex items-center gap-2 px-3 py-1.5 rounded-full border text-xs transition-all ${
                      isActive
                        ? 'border-[#38BDF8] bg-[rgba(56,189,248,0.12)] text-[#E2E8F0] shadow-[0_0_10px_rgba(56,189,248,0.4)]'
                        : 'border-[rgba(148,163,184,0.5)] text-[#94A3B8] hover:border-[#38BDF8] hover:text-[#E2E8F0]'
                    }`}
                  >
                    <span
                      className={`w-5 h-5 rounded-full flex items-center justify-center text-[11px] ${
                        isActive
                          ? 'bg-[#38BDF8] text-[#0F172A]'
                          : 'bg-[rgba(15,23,42,0.8)] text-[#94A3B8]'
                      }`}
                    >
                      {round}
                    </span>
                    <span>{label.title}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Main Debate Layout */}
        <div ref={mainStageRef} className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Motion Focus */}
          <div className="flex flex-col items-center text-center mb-4">
            <motion.div
              className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full border border-[rgba(56,189,248,0.4)] bg-[rgba(15,23,42,0.7)] shadow-[0_0_20px_rgba(56,189,248,0.35)]"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3 }}
            >
              <Scale size={16} className="text-[#38BDF8]" />
              <span className="text-xs text-[#E2E8F0]">
                英式议会风格（两方制） · 三轮交锋
              </span>
            </motion.div>
            <div className="mt-4 max-w-3xl">
              {motionInput.trim() ? (
                <h2 className="text-xl text-gradient mb-2">
                  {motionInput.trim()}
                </h2>
              ) : (
                <h2 className="text-xl text-gradient mb-2">
                  在这里输入辩题，构建你的思维对撞场
                </h2>
              )}
              <p className="text-[#94A3B8] text-sm">
                系统通过多轮正反交锋，帮助你从不同角度思考问题。辩论结果用于激发灵感，而非给出唯一答案。
              </p>
            </div>
          </div>

          {currentRoundsCount > 0 && (
            <div className="text-center text-xs text-[#94A3B8] mb-2">
              本场辩论已完成 {currentRoundsCount} 轮，你可以通过上方轮次切换查看正反双方的详细发言。
            </div>
          )}

          {/* Affirmative vs Opposition Columns */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Affirmative */}
            <motion.div
              className="glass-strong rounded-2xl p-5 border border-[rgba(34,197,94,0.35)] shadow-lg"
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3 }}
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-[#22c55e] to-[#22d3ee] flex items-center justify-center">
                    <span className="text-xs font-semibold text-[#0F172A]">
                      正
                    </span>
                  </div>
                  <div>
                    <p className="text-sm text-[#E2E8F0] font-medium">
                      正方（Affirmative）
                    </p>
                    <p className="text-xs text-[#94A3B8]">
                      围绕辩题，构建支持该观点的立论与理由
                    </p>
                  </div>
                </div>
              </div>
              <div className="mt-2 text-sm text-[#cbd5f5] space-y-2">
                <p>
                  当前轮次：
                  <span className="text-[#22c55e] ml-1">
                    {ROUND_LABELS[activeRound].title}
                  </span>
                  <span className="ml-1 text-[#64748B]">
                    ({ROUND_LABELS[activeRound].subtitle})
                  </span>
                </p>
                {(() => {
                  const round = findCurrentRound();
                  const streamingText = streamAffirmative[activeRound];
                  if (!round && !streamingText) {
                    return (
                      <p>
                        后续版本中，系统会在每一轮为正方生成结构化发言内容。当前版本侧重于帮助你规划辩题和回合结构。
                      </p>
                    );
                  }
                  return (
                    <div className="mt-3 text-sm">
                      <DebateMarkdown
                        content={streamingText || round?.affirmative_speech || ''}
                        side="affirmative"
                      />
                    </div>
                  );
                })()}
              </div>
            </motion.div>

            {/* Opposition */}
            <motion.div
              className="glass-strong rounded-2xl p-5 border border-[rgba(129,140,248,0.4)] shadow-lg"
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3 }}
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-[#6366f1] to-[#ec4899] flex items-center justify-center">
                    <span className="text-xs font-semibold text-[#0F172A]">
                      反
                    </span>
                  </div>
                  <div>
                    <p className="text-sm text-[#E2E8F0] font-medium">
                      反方（Opposition）
                    </p>
                    <p className="text-xs text-[#94A3B8]">
                      从不同角度挑战辩题，提出质疑与反例
                    </p>
                  </div>
                </div>
              </div>
              <div className="mt-2 text-sm text-[#cbd5f5] space-y-2">
                <p>
                  当前轮次：
                  <span className="text-[#6366f1] ml-1">
                    {ROUND_LABELS[activeRound].title}
                  </span>
                  <span className="ml-1 text-[#64748B]">
                    ({ROUND_LABELS[activeRound].subtitle})
                  </span>
                </p>
                {(() => {
                  const round = findCurrentRound();
                  const streamingText = streamOpposition[activeRound];
                  if (!round && !streamingText) {
                    return (
                      <p>
                        正反双方的观点交锋，旨在帮助你发现盲点与潜在反驳，而不是给出某个"标准答案"。
                      </p>
                    );
                  }
                  return (
                    <div className="mt-3 text-sm">
                      <DebateMarkdown
                        content={streamingText || round?.opposition_speech || ''}
                        side="opposition"
                      />
                    </div>
                  );
                })()}
              </div>
            </motion.div>
          </div>

          {hasFinalJudgeDecision && (
            <motion.div
              className="glass-strong rounded-2xl p-5 border border-[rgba(251,191,36,0.4)] shadow-lg"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3 }}
            >
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-[#fbbf24] to-[#f97316] flex items-center justify-center">
                  <Scale size={16} className="text-[#0F172A]" />
                </div>
                <div>
                  <p className="text-sm text-[#E2E8F0] font-medium">
                    裁判评议 · 最终裁决
                  </p>
                  <p className="text-xs text-[#94A3B8]">
                    第 3 轮 · {ROUND_LABELS[3].title}
                  </p>
                </div>
              </div>
              <div className="mt-4 space-y-4 text-sm">
                <div>
                  <p className="text-xs uppercase tracking-[0.2em] text-[#94A3B8]">
                    综合评议
                  </p>
                  <div className="mt-2">
                    {finalJudgeNotes ? (
                      <DebateMarkdown content={finalJudgeNotes} side="judge" />
                    ) : (
                      <p className="text-[#E5E7EB]">
                        {isRunningDebate ? '评议生成中…' : '（暂无评议）'}
                      </p>
                    )}
                  </div>
                </div>
                <div>
                  <p className="text-xs uppercase tracking-[0.2em] text-[#94A3B8]">
                    最终裁决
                  </p>
                  <div className="mt-2 text-[#fbbf24]">
                    {finalJudgeAnswer ? (
                      <DebateMarkdown content={finalJudgeAnswer} side="judge" />
                    ) : (
                      <p>{isRunningDebate ? '裁决生成中…' : '（暂无裁决）'}</p>
                    )}
                  </div>
                </div>
              </div>
            </motion.div>
          )}

          {/* Hint Card */}
          <div className="glass-strong rounded-2xl p-4 border border-[rgba(148,163,184,0.4)] flex items-start gap-3 text-sm text-[#cbd5f5]">
            <Brain size={18} className="text-[#34D399] mt-1" />
            <div>
              <p className="font-medium text-[#E2E8F0] mb-1">使用提示</p>
              <p className="text-[#94A3B8]">
                你可以先在下方整理好辩题，再逐步完善每一轮希望看到的思考方向。
                然后选择模型并运行辩论，系统会自动为每一轮生成正反双方的观点，用于激发多角度思考。
              </p>
            </div>
          </div>
        </div>

        {/* Bottom: Motion Input */}
        <div className="border-t border-[rgba(56,189,248,0.15)] px-6 py-4 glass">
          <div className="flex flex-col gap-3">
            <label className="text-sm text-[#E2E8F0] flex items-center gap-2">
              <MessageSquare size={16} className="text-[#38BDF8]" />
              <span>设置当前辩题</span>
            </label>
            <div className="flex gap-3">
              <textarea
                value={motionInput}
                onChange={e => setMotionInput(e.target.value)}
                placeholder="例如：人工智能在未来二十年内会整体提升而非削弱人类的创造力。"
                className="flex-1 min-h-[72px] max-h-[120px] glass-strong border border-[rgba(148,163,184,0.5)] rounded-xl px-4 py-2 text-sm text-[#E2E8F0] placeholder:text-[#64748B] focus:outline-none focus:ring-2 focus:ring-[#38BDF8] resize-none"
              />
              <div className="flex flex-col gap-2">
                <motion.button
                  type="button"
                  onClick={handleSaveMotion}
                  className="px-4 py-2 rounded-xl bg-gradient-to-r from-[#38BDF8] to-[#0EA5E9] text-[#0F172A] text-sm font-medium shadow-[0_0_15px_rgba(56,189,248,0.6)]"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  保存辩题
                </motion.button>
                <motion.button
                  type="button"
                  onClick={handleRunDebate}
                  disabled={isRunningDebate}
                  className="px-4 py-2 rounded-xl glass-strong text-sm font-medium border border-[rgba(56,189,248,0.6)] text-[#E2E8F0] hover:bg-[rgba(56,189,248,0.12)] disabled:opacity-60 disabled:cursor-not-allowed"
                  whileHover={{ scale: isRunningDebate ? 1 : 1.02 }}
                  whileTap={{ scale: isRunningDebate ? 1 : 0.98 }}
                >
                  {isRunningDebate ? '正在运行辩论…' : '开始辩论'}
                </motion.button>
                <button
                  type="button"
                  onClick={() => {
                    setMotionInput('');
                    setActiveRound(1);
                    setCurrentSessionId(null);
                  }}
                  className="px-4 py-2 rounded-xl glass text-xs text-[#94A3B8] hover:bg-[rgba(15,23,42,0.9)] border border-[rgba(148,163,184,0.4)]"
                >
                  清空当前输入
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
