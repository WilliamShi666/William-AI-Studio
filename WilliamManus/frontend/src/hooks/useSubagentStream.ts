import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useShadowCloneStore } from '@/lib/stores/shadow-clone-store';
import type { UnifiedMessage } from '@/components/thread/types';

const MIN_INDICATOR_MS = 800;
const IDLE_TIMEOUT_MS = 1000;

export interface UseSubagentStreamResult {
  status: string;
  textContent: string;
  reasoningContent: string;
  isWritingFile: boolean;
  messages: UnifiedMessage[];
  toolCall: null; // Subagent tool calls render via ThreadContent's existing path
  error: string | null;
  startStreaming: (subtaskId: string) => void;
  stopStreaming: () => void;
}

/**
 * Lightweight streaming hook for an inspected Subagent.
 *
 * Manages per-subagent streaming state (text, reasoning, indicator)
 * backed by the shadow-clone-store's inspectionStreamStates map.
 * Reuses the same indicator timing logic as useAgentStream:
 *  - Indicator starts after 1s idle (no new text)
 *  - Minimum 800ms display
 */
export function useSubagentStream(subtaskId: string | null): UseSubagentStreamResult {
  const [status, setStatus] = useState<string>('idle');
  const [textContent, setTextContent] = useState<string>('');
  const [reasoningContent, setReasoningContent] = useState<string>('');
  const [isWritingFile, setIsWritingFile] = useState<boolean>(false);
  const [messages, setMessages] = useState<UnifiedMessage[]>([]);
  const [error, setError] = useState<string | null>(null);

  const indicatorStartRef = useRef<number | null>(null);
  const indicatorTimerRef = useRef<NodeJS.Timeout | null>(null);
  const idleTimerRef = useRef<NodeJS.Timeout | null>(null);
  const isMountedRef = useRef<boolean>(true);
  const activeSubtaskRef = useRef<string | null>(null);

  // Sync from store when subtaskId changes
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (indicatorTimerRef.current) clearTimeout(indicatorTimerRef.current);
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!subtaskId) {
      setStatus('idle');
      setTextContent('');
      setReasoningContent('');
      setIsWritingFile(false);
      setMessages([]);
      setError(null);
      activeSubtaskRef.current = null;
      return;
    }

    activeSubtaskRef.current = subtaskId;
    const store = useShadowCloneStore.getState();
    const existing = store.inspectionStreamStates[subtaskId];

    if (existing) {
      setStatus(existing.status);
      setTextContent(existing.textContent);
      setReasoningContent(existing.reasoningContent);
      setIsWritingFile(existing.isWritingFile);
      setMessages(existing.messages);
    } else {
      store.initSubagentInspection(subtaskId);
      setStatus('connecting');
      setTextContent('');
      setReasoningContent('');
      setIsWritingFile(false);
      setMessages([]);
    }

    // Subscribe to store updates for this subtask
    const unsubscribe = useShadowCloneStore.subscribe((state) => {
      if (!isMountedRef.current) return;
      if (activeSubtaskRef.current !== subtaskId) return;
      const state_ = state.inspectionStreamStates[subtaskId];
      if (!state_) return;

      setStatus(state_.status);
      setTextContent(state_.textContent);
      setReasoningContent(state_.reasoningContent);
      setIsWritingFile(state_.isWritingFile);
      setMessages(state_.messages);
    });

    return () => {
      unsubscribe();
    };
  }, [subtaskId]);

  // Wire idle detection for indicator (same logic as useAgentStream stopDetectionTimerRef)
  // When text content arrives, reset the idle timer. After 1s of no new text,
  // start the blue bar indicator (FR-008).
  useEffect(() => {
    if (!subtaskId) return;
    if (!textContent.trim()) {
      // No content yet — clear timer, don't show indicator
      if (idleTimerRef.current) {
        clearTimeout(idleTimerRef.current);
        idleTimerRef.current = null;
      }
      return;
    }

    // New text arrived — reset idle timer
    if (idleTimerRef.current) {
      clearTimeout(idleTimerRef.current);
    }
    idleTimerRef.current = setTimeout(() => {
      if (!isMountedRef.current) return;
      if (!isWritingFile) {
        startIndicator();
      }
    }, IDLE_TIMEOUT_MS);

    return () => {
      if (idleTimerRef.current) {
        clearTimeout(idleTimerRef.current);
        idleTimerRef.current = null;
      }
    };
  }, [textContent, subtaskId, isWritingFile, startIndicator]);

  // Stop indicator when streaming completes
  useEffect(() => {
    if (!subtaskId) return;
    if (status === 'completed' || status === 'error' || status === 'failed' || status === 'stopped') {
      stopIndicator();
    }
  }, [status, subtaskId, stopIndicator]);

  // Idle timer → indicator control (same logic as useAgentStream)
  const startIndicator = useCallback(() => {
    indicatorStartRef.current = Date.now();
    if (indicatorTimerRef.current) clearTimeout(indicatorTimerRef.current);
    setIsWritingFile(true);
  }, []);

  const stopIndicator = useCallback(() => {
    const startedAt = indicatorStartRef.current;
    if (!startedAt) {
      setIsWritingFile(false);
      return;
    }
    const elapsed = Date.now() - startedAt;
    const remaining = MIN_INDICATOR_MS - elapsed;
    if (remaining <= 0) {
      indicatorStartRef.current = null;
      setIsWritingFile(false);
      return;
    }
    if (indicatorTimerRef.current) clearTimeout(indicatorTimerRef.current);
    indicatorTimerRef.current = setTimeout(() => {
      indicatorStartRef.current = null;
      setIsWritingFile(false);
      indicatorTimerRef.current = null;
    }, remaining);
  }, []);

  const startStreaming = useCallback(
    (id: string) => {
      useShadowCloneStore.getState().initSubagentInspection(id);
      setStatus('connecting');
    },
    [],
  );

  const stopStreaming = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    stopIndicator();
    setStatus('idle');
  }, [stopIndicator]);

  return {
    status,
    textContent,
    reasoningContent,
    isWritingFile,
    messages,
    toolCall: null,
    error,
    startStreaming,
    stopStreaming,
  };
}
