export const SYNTHETIC_TERMINAL_ASSISTANT_KIND = 'terminal_stream_recovery';

const safeJsonParse = (value, fallback) => {
  if (value && typeof value === 'object') {
    return value;
  }
  if (typeof value !== 'string') {
    return fallback;
  }
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
};

const toMetadataObject = (message) => {
  const metadata = safeJsonParse(message?.metadata, {});
  return metadata && typeof metadata === 'object' ? metadata : {};
};

const toContentObject = (message) => {
  const content = safeJsonParse(message?.content, null);
  return content && typeof content === 'object' ? content : null;
};

const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();

export const getRenderableAssistantText = (message) => {
  if (!message || message.type !== 'assistant') return '';
  const contentObject = toContentObject(message);
  if (contentObject) {
    const content = contentObject.content ?? contentObject.text ?? '';
    if (typeof content === 'string' || typeof content === 'number') {
      return normalizeText(content);
    }
  }
  return normalizeText(message.content);
};

export const getAssistantRunId = (message) => {
  const metadata = toMetadataObject(message);
  if (typeof metadata.thread_run_id === 'string' && metadata.thread_run_id) {
    return metadata.thread_run_id;
  }
  if (typeof metadata.agent_run_id === 'string' && metadata.agent_run_id) {
    return metadata.agent_run_id;
  }
  const content = toContentObject(message);
  if (typeof content?.thread_run_id === 'string' && content.thread_run_id) {
    return content.thread_run_id;
  }
  if (typeof content?.agent_run_id === 'string' && content.agent_run_id) {
    return content.agent_run_id;
  }
  return null;
};

export const isSyntheticTerminalAssistantMessage = (message) => {
  if (!message || message.type !== 'assistant') return false;
  const metadata = toMetadataObject(message);
  return metadata.synthetic_kind === SYNTHETIC_TERMINAL_ASSISTANT_KIND;
};

export const buildSyntheticTerminalAssistantMessage = ({
  runId,
  threadId,
  textContent,
  reasoningContent = '',
  createdAt = new Date().toISOString(),
}) => {
  const normalizedRunId = String(runId || '').trim();
  const content = {
    role: 'assistant',
    content: String(textContent || ''),
  };
  if (String(reasoningContent || '').trim()) {
    content.reasoning_content = String(reasoningContent);
  }

  return {
    message_id: `stream-terminal-recovery-${normalizedRunId || createdAt}`,
    thread_id: threadId,
    type: 'assistant',
    is_llm_message: true,
    content: JSON.stringify(content),
    metadata: JSON.stringify({
      source: 'stream_terminal_reconciliation',
      synthetic_kind: SYNTHETIC_TERMINAL_ASSISTANT_KIND,
      thread_run_id: normalizedRunId || null,
      stream_status: 'terminal_recovery',
    }),
    created_at: createdAt,
    updated_at: createdAt,
  };
};

const normalizeFetchedMessage = (message, threadId) => {
  if (!message) return null;
  const now = new Date().toISOString();
  return {
    ...message,
    message_id: message.message_id || message.id || null,
    thread_id: message.thread_id || threadId,
    type: message.type || 'system',
    is_llm_message: Boolean(message.is_llm_message),
    content:
      typeof message.content === 'string'
        ? message.content
        : JSON.stringify(message.content ?? ''),
    metadata:
      typeof message.metadata === 'string'
        ? message.metadata
        : JSON.stringify(message.metadata ?? {}),
    created_at: message.created_at || now,
    updated_at: message.updated_at || message.created_at || now,
  };
};

const getTime = (message) => {
  const value = Date.parse(message?.created_at || '');
  return Number.isFinite(value) ? value : 0;
};

const shouldDropLocalSyntheticAssistant = (message, serverAssistantFacts) => {
  if (!isSyntheticTerminalAssistantMessage(message)) return false;

  const runId = getAssistantRunId(message);
  if (runId && serverAssistantFacts.runIds.has(runId)) {
    return true;
  }

  const text = getRenderableAssistantText(message);
  return Boolean(text && serverAssistantFacts.texts.has(text));
};

export const hasCanonicalAssistantForRun = (messages, runId) => {
  const normalizedRunId = String(runId || '').trim();
  if (!normalizedRunId) return false;
  return (messages || []).some((message) => {
    if (!message || message.type !== 'assistant') return false;
    if (isSyntheticTerminalAssistantMessage(message)) return false;
    return getAssistantRunId(message) === normalizedRunId;
  });
};

export const mergeTerminalReconciledMessages = (
  previousMessages,
  fetchedMessages,
  options = {},
) => {
  const threadId = options.threadId;
  const previousById = new Map(
    (previousMessages || [])
      .filter((message) => message?.message_id)
      .map((message) => [message.message_id, message]),
  );

  const normalizedServerMessages = (fetchedMessages || [])
    .map((message) => normalizeFetchedMessage(message, threadId))
    .filter(Boolean)
    .map((message) => {
      const previous = previousById.get(message.message_id);
      if (
        previous &&
        message.type === 'assistant' &&
        !normalizeText(getRenderableAssistantText(message)) &&
        normalizeText(getRenderableAssistantText(previous))
      ) {
        return {
          ...message,
          content: previous.content,
          metadata: previous.metadata ?? message.metadata,
          updated_at: previous.updated_at || message.updated_at,
        };
      }
      return message;
    });

  const serverIds = new Set(
    normalizedServerMessages
      .map((message) => message.message_id)
      .filter(Boolean),
  );
  const serverAssistantFacts = normalizedServerMessages.reduce(
    (facts, message) => {
      if (message.type !== 'assistant') return facts;
      const text = getRenderableAssistantText(message);
      if (!text) return facts;
      const runId = getAssistantRunId(message);
      if (runId) facts.runIds.add(runId);
      facts.texts.add(text);
      return facts;
    },
    { runIds: new Set(), texts: new Set() },
  );

  const localExtras = (previousMessages || []).filter((message) => {
    if (shouldDropLocalSyntheticAssistant(message, serverAssistantFacts)) {
      return false;
    }
    if (!message.message_id) return true;
    if (serverIds.has(message.message_id)) return false;
    return true;
  });

  return [...normalizedServerMessages, ...localExtras].sort((left, right) => {
    const timeDelta = getTime(left) - getTime(right);
    if (timeDelta !== 0) return timeDelta;
    return String(left.message_id || '').localeCompare(String(right.message_id || ''));
  });
};
