import {
  getAssistantRunId,
  getRenderableAssistantText,
  isSyntheticTerminalAssistantMessage,
} from '../../../../../../hooks/useAgentStream-terminal-reconciliation.js';

export function normalizeThreadMessages(rawMessages, threadId) {
  return (rawMessages || [])
    .filter((message) => message.type !== 'status')
    .map((message) => ({
      message_id: message.message_id || null,
      thread_id: message.thread_id || threadId,
      type: message.type || 'system',
      is_llm_message: Boolean(message.is_llm_message),
      content: message.content || '',
      metadata: message.metadata || '{}',
      created_at: message.created_at || new Date().toISOString(),
      updated_at: message.updated_at || new Date().toISOString(),
      agent_id: message.agent_id,
      agents: message.agents,
    }));
}

export function mergeThreadMessages(previousMessages, serverMessages, now = Date.now()) {
  const previousById = new Map(
    (previousMessages || [])
      .filter((message) => message && message.message_id)
      .map((message) => [message.message_id, message]),
  );

  const mergedServerMessages = (serverMessages || []).map((message) => {
    if (!message || !message.message_id) return message;

    const previousMessage = previousById.get(message.message_id);
    if (!previousMessage) return message;

    if (
      message.type === 'assistant' &&
      (!message.content || String(message.content).trim() === '') &&
      previousMessage.content &&
      String(previousMessage.content).trim() !== ''
    ) {
      return {
        ...message,
        content: previousMessage.content,
        metadata: previousMessage.metadata ?? message.metadata,
        updated_at: previousMessage.updated_at || message.updated_at,
      };
    }

    return message;
  });

  const serverIds = new Set(
    mergedServerMessages.map((message) => message.message_id).filter(Boolean),
  );
  const canonicalAssistantFacts = mergedServerMessages.reduce(
    (facts, message) => {
      if (!message || message.type !== 'assistant') {
        return facts;
      }

      const text = getRenderableAssistantText(message);
      if (!text) {
        return facts;
      }

      const runId = getAssistantRunId(message);
      if (runId) {
        facts.runIds.add(runId);
      }
      facts.texts.add(text);
      return facts;
    },
    { runIds: new Set(), texts: new Set() },
  );
  const localExtras = (previousMessages || []).filter((message) => {
    if (isSyntheticTerminalAssistantMessage(message)) {
      const runId = getAssistantRunId(message);
      const text = getRenderableAssistantText(message);
      if (runId && canonicalAssistantFacts.runIds.has(runId)) {
        return false;
      }
      if (text && canonicalAssistantFacts.texts.has(text)) {
        return false;
      }
    }
    if (!message.message_id) return true;
    if (typeof message.message_id === 'string' && message.message_id.startsWith('temp-')) {
      return true;
    }
    if (!serverIds.has(message.message_id)) return true;
    return false;
  });

  return [...mergedServerMessages, ...localExtras].sort((leftMessage, rightMessage) => {
    const leftTime = leftMessage.created_at ? new Date(leftMessage.created_at).getTime() : 0;
    const rightTime = rightMessage.created_at ? new Date(rightMessage.created_at).getTime() : 0;
    return leftTime - rightTime;
  });
}

export function areThreadMessagesEqual(leftMessages, rightMessages) {
  if (leftMessages.length !== rightMessages.length) return false;
  return leftMessages.every((leftMessage, index) => {
    const rightMessage = rightMessages[index];
    return Boolean(rightMessage) &&
      leftMessage.message_id === rightMessage.message_id &&
      leftMessage.content === rightMessage.content &&
      leftMessage.type === rightMessage.type &&
      leftMessage.metadata === rightMessage.metadata;
  });
}
