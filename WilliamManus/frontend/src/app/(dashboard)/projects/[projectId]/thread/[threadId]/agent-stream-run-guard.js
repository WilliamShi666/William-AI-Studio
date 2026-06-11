const ACTIVE_AGENT_STATUSES = new Set(['running', 'connecting']);

export function shouldApplyTerminalStatus({
  statusRunId,
  activeRunId,
  isSending,
  userInitiatedRun,
  agentStatus,
}) {
  const hasActiveRunSignals =
    isSending ||
    userInitiatedRun ||
    ACTIVE_AGENT_STATUSES.has(agentStatus);

  if (statusRunId && activeRunId && statusRunId !== activeRunId) {
    return false;
  }

  if (!statusRunId && activeRunId && hasActiveRunSignals) {
    return false;
  }

  return true;
}

export function shouldAdoptLatestRunningRun({
  latestRunningRunId,
  currentAgentRunId,
  agentStatus,
  isStreamingOrRecentlyStreamed,
}) {
  if (!latestRunningRunId) {
    return false;
  }

  if (currentAgentRunId === latestRunningRunId) {
    return false;
  }

  const hasActiveRunSignals =
    Boolean(currentAgentRunId) ||
    ACTIVE_AGENT_STATUSES.has(agentStatus) ||
    isStreamingOrRecentlyStreamed;

  return !hasActiveRunSignals;
}


function parseMetadata(metadata) {
  if (!metadata) {
    return {};
  }

  if (typeof metadata === 'object') {
    return metadata;
  }

  if (typeof metadata !== 'string') {
    return {};
  }

  try {
    const parsed = JSON.parse(metadata);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (_error) {
    return {};
  }
}

function getMessageRunId(message) {
  const metadata = parseMetadata(message?.metadata);
  return metadata.thread_run_id || metadata.agent_run_id || metadata.run_id || null;
}

function parseContent(content) {
  if (content && typeof content === 'object') {
    return content;
  }

  if (typeof content !== 'string') {
    return null;
  }

  try {
    return JSON.parse(content);
  } catch (_error) {
    return content;
  }
}

function hasRenderableAssistantContent(message) {
  if (message?.type !== 'assistant') {
    return false;
  }

  const parsedContent = parseContent(message.content);
  if (typeof parsedContent === 'string') {
    const trimmed = parsedContent.trim();
    return Boolean(trimmed && trimmed !== '(empty message)');
  }

  if (!parsedContent || typeof parsedContent !== 'object') {
    return false;
  }

  const textContent =
    typeof parsedContent.content === 'string'
      ? parsedContent.content
      : typeof parsedContent.text === 'string'
        ? parsedContent.text
        : '';
  const reasoningContent =
    typeof parsedContent.reasoning_content === 'string'
      ? parsedContent.reasoning_content
      : '';
  const trimmedText = textContent.trim();

  return Boolean(
    (trimmedText && trimmedText !== '(empty message)') ||
      reasoningContent.trim(),
  );
}

export function hasAssistantMessageForRun({
  messages,
  terminalRunId,
  activeRunId,
  runStartedAt,
}) {
  return (messages || []).some((message) => {
    if (message.type !== 'assistant') return false;
    if (!hasRenderableAssistantContent(message)) return false;

    const messageRunId = getMessageRunId(message);
    const expectedRunId = terminalRunId || activeRunId || null;
    if (messageRunId) {
      return Boolean(expectedRunId && messageRunId === expectedRunId);
    }

    if (!runStartedAt) {
      return !expectedRunId;
    }
    const createdAt = message.created_at ? new Date(message.created_at).getTime() : 0;
    return createdAt >= runStartedAt - 1000;
  });
}

export function shouldShowCompletionHint({
  terminalRunId,
  activeRunId,
  hasSeenRun,
  hasAssistantMessageAfterRunStart,
  hasStreamingContent,
  finalStatus,
}) {
  if (!hasSeenRun) {
    return false;
  }

  if (!['completed', 'stopped', 'failed', 'error', 'agent_not_running'].includes(finalStatus)) {
    return false;
  }

  if (activeRunId && terminalRunId && activeRunId !== terminalRunId) {
    return false;
  }

  if (!hasAssistantMessageAfterRunStart) {
    return false;
  }

  return !hasStreamingContent;
}

export function shouldRetryTerminalMessageRefetch({
  terminalRunId,
  activeRunId,
  isTerminalStatus,
  hasAssistantMessageAfterRunStart,
  attempt,
  maxAttempts,
}) {
  if (!isTerminalStatus || !terminalRunId) {
    return false;
  }

  if (activeRunId && terminalRunId && activeRunId !== terminalRunId) {
    return false;
  }

  if (hasAssistantMessageAfterRunStart) {
    return false;
  }

  return attempt < maxAttempts;
}
